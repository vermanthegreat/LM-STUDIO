"""Knowledge subsystem: storage, routing, extraction, classification, persistence."""

from __future__ import annotations

import json
from pathlib import Path

import db
import pytest

from knowledge import repository as repo
from knowledge.classification import classify_text
from knowledge.extractors import decode_text, extract_docx, extract_pdf, extract_plain_text
from knowledge.ingestion import UploadedFile
from knowledge.router import DOCX_MIME, PDF_MIME, route_file
from knowledge.schemas import (
    ClassificationStatus,
    ContentKind,
    ExtractionStatus,
    IngestStatus,
    VisionResult,
    VisionStatus,
)
from knowledge.storage import OriginalFileStore, safe_extension, sanitize_filename, sha256_hex
from knowledge.vision import VisionOutcome, vision_result_to_text
from tests.knowledge_support import (
    JPEG_HEADER,
    PNG_1X1,
    WEBP_HEADER,
    classification_json,
    make_blank_pdf,
    make_docx,
    make_ingestor,
    make_pdf,
    scripted_chat,
)


# --------------------------------------------------------------------- storage


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("../../etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\evil.dll", "evil.dll"),
        ("/abs/path/report.pdf", "report.pdf"),
        ("..", "upload"),
        ("", "upload"),
        ("a\x00b<c>.txt", "a_b_c_.txt"),
        ("  .hidden  ", "hidden"),
    ],
)
def test_sanitize_filename_strips_directories_and_unsafe_chars(raw, expected):
    assert sanitize_filename(raw) == expected


def test_sanitize_filename_bounds_length_and_keeps_extension():
    name = sanitize_filename("x" * 500 + ".md")
    assert len(name) <= 200
    assert name.endswith(".md")


def test_safe_extension_rejects_weird_extensions():
    assert safe_extension("notes.MD") == "md"
    assert safe_extension("file.tar.gz") == "gz"
    assert safe_extension("file.p$p") == ""
    assert safe_extension("noext") == ""


def test_original_store_is_content_addressed_and_idempotent(tmp_path):
    store = OriginalFileStore(tmp_path / "store")
    data = b"hello knowledge"
    digest = sha256_hex(data)
    first = store.save(data, content_hash=digest, extension="txt")
    second = store.save(data, content_hash=digest, extension="txt")
    assert first.created is True
    assert second.created is False
    assert first.relative_path == f"originals/{digest[:2]}/{digest}.txt"
    assert first.absolute_path.read_bytes() == data
    assert len(list((tmp_path / "store").rglob("*.txt"))) == 1


def test_original_store_refuses_escape_and_hash_mismatch(tmp_path):
    store = OriginalFileStore(tmp_path / "store")
    with pytest.raises(ValueError):
        store.resolve("../../outside.txt")
    with pytest.raises(ValueError):
        store.save(b"abc", content_hash="0" * 64, extension="txt")
    with pytest.raises(ValueError):
        store.relative_path_for("../evil", "txt")


# --------------------------------------------------------------------- routing


@pytest.mark.parametrize(
    "filename,data,kind,mime",
    [
        ("a.png", PNG_1X1, ContentKind.IMAGE, "image/png"),
        ("photo.jpg", JPEG_HEADER, ContentKind.IMAGE, "image/jpeg"),
        ("shot.webp", WEBP_HEADER, ContentKind.IMAGE, "image/webp"),
        ("misnamed.txt", PNG_1X1, ContentKind.IMAGE, "image/png"),
        ("doc.pdf", make_pdf(["hello"]), ContentKind.DOCUMENT, PDF_MIME),
        ("doc.docx", make_docx(heading="H", paragraphs=["p"]), ContentKind.DOCUMENT, DOCX_MIME),
        ("notes.txt", b"plain text", ContentKind.TEXT, "text/plain"),
        ("readme.md", b"# Title", ContentKind.TEXT, "text/markdown"),
        ("data.json", b'{"a": 1}', ContentKind.TEXT, "application/json"),
        ("main.py", b"print('x')\n", ContentKind.TEXT, "text/x-python"),
        ("noext", b"just words", ContentKind.TEXT, "text/plain"),
    ],
)
def test_route_file_is_deterministic(filename, data, kind, mime):
    decision = route_file(filename, data)
    assert decision.kind == kind
    assert decision.mime_type == mime


@pytest.mark.parametrize(
    "filename,data",
    [
        ("tool.exe", b"MZ\x90\x00\x03\x00\x00\x00binary\x00\x00"),
        ("fake.png", b"this is not a png"),
        ("fake.pdf", b"not a pdf at all"),
        ("archive.zip", b"PK\x03\x04garbage"),
        ("binary.txt", b"\x00\x01\x02\x03\x00binary"),
    ],
)
def test_route_file_rejects_unsupported_and_mismatched_content(filename, data):
    assert route_file(filename, data).kind == ContentKind.UNSUPPORTED


# --------------------------------------------------------------------- extraction


def test_decode_text_handles_bom_and_legacy_encodings():
    assert decode_text("é".encode("utf-8-sig"))[0] == "é"
    assert decode_text("šđ".encode("utf-16"))[0] == "šđ"
    assert decode_text("café".encode("cp1252")) == ("café", "cp1252")


def test_json_extraction_pretty_prints_and_flags_invalid():
    ok = extract_plain_text(b'{"b":[1,2],"a":"x"}', "application/json")
    assert ok.status == ExtractionStatus.OK
    assert '"b": [' in ok.raw_text
    bad = extract_plain_text(b"{not json", "application/json")
    assert bad.status == ExtractionStatus.OK
    assert "json_invalid_kept_as_text" in bad.warnings


def test_pdf_text_extraction():
    pdf = make_pdf(["Benchmark results for 5000 products", "Throughput doubled after caching"])
    result = extract_pdf(pdf)
    assert result.status == ExtractionStatus.OK
    assert "5000 products" in result.raw_text
    assert result.metadata["page_count"] == 1


def test_pdf_without_text_layer_requires_vision_not_silent_empty():
    result = extract_pdf(make_blank_pdf())
    assert result.status == ExtractionStatus.NEEDS_VISION
    assert "pdf_little_or_no_text_layer" in result.warnings


def test_corrupt_pdf_fails_cleanly():
    result = extract_pdf(b"%PDF-1.4\ngarbage without structure")
    assert result.status in {ExtractionStatus.FAILED, ExtractionStatus.NEEDS_VISION}


def test_docx_extraction_preserves_structure_order():
    docx = make_docx(
        heading="Quarterly Review",
        paragraphs=["First paragraph.", "Second paragraph."],
        bullets=["Point one"],
        table=[["Name", "Role"], ["Nikolay Galinov", "Engineer"]],
    )
    result = extract_docx(docx)
    assert result.status == ExtractionStatus.OK
    text = result.raw_text
    assert text.startswith("# Quarterly Review")
    assert "- Point one" in text
    assert "Nikolay Galinov | Engineer" in text
    assert text.index("First paragraph.") < text.index("Second paragraph.") < text.index("Name | Role")


def test_docx_corrupt_zip_fails_cleanly():
    assert extract_docx(b"PK\x03\x04broken").status == ExtractionStatus.FAILED


# --------------------------------------------------------------------- classification


def _classify(raw):
    return classify_text("Some document text.", filename="a.txt", chat_fn=lambda *_a, **_k: raw)


def test_classification_valid_output():
    out = _classify(
        classification_json(
            summary="  A   summary ",
            topics=["OAuth", "oauth", "  CommerceGov "],
            entities=[{"name": "Nikolay Galinov", "type": "PERSON"}, "Acme", {"name": ""}],
            event_date="2026-07-14",
            importance=0.8,
            extra_key="ignored",
        )
    )
    assert out.status == ClassificationStatus.OK
    cls = out.classification
    assert cls.summary == "A summary"
    assert cls.topics == ["OAuth", "CommerceGov"]
    assert [(e.name, e.type.value) for e in cls.entities] == [("Nikolay Galinov", "person"), ("Acme", "other")]
    assert cls.event_date.isoformat() == "2026-07-14"


def test_classification_accepts_fenced_json():
    out = _classify("```json\n" + classification_json() + "\n```")
    assert out.status == ClassificationStatus.OK


@pytest.mark.parametrize(
    "raw,warning_prefix",
    [
        ("not json at all", "llm_output_not_json"),
        (classification_json(importance=5), "llm_output_schema_invalid"),
        (classification_json(topics="a,b,c"), "llm_output_schema_invalid"),
        ("[1, 2, 3]", "llm_output_not_json"),
    ],
)
def test_classification_rejects_malformed_output(raw, warning_prefix):
    out = _classify(raw)
    assert out.status == ClassificationStatus.INVALID_OUTPUT
    assert out.classification is None
    assert out.warning.startswith(warning_prefix)


def test_classification_drops_vague_event_dates():
    out = _classify(classification_json(event_date="next week"))
    assert out.status == ClassificationStatus.OK
    assert out.classification.event_date is None


def test_classification_unavailable_and_empty_text():
    assert _classify(None).status == ClassificationStatus.UNAVAILABLE
    assert classify_text("   ", filename="a", chat_fn=lambda *_a, **_k: "{}").status == (
        ClassificationStatus.SKIPPED_NO_TEXT
    )


def test_classification_prompt_is_bounded():
    seen = {}

    def chat(messages, **_k):
        seen["user"] = messages[-1]["content"]
        return classification_json()

    classify_text("x" * 100_000, filename="big.txt", chat_fn=chat)
    assert len(seen["user"]) < 10_000


# --------------------------------------------------------------------- ingestion + persistence


def _item(ingestor, item_id):
    with db.get_conn(ingestor.database_path) as conn:
        return repo.get_item(conn, item_id, include_text=True)


def test_text_ingestion_persists_text_classification_topics_entities(tmp_path):
    chat = scripted_chat(
        lambda _u: classification_json(
            summary="Claude findings about the ChatGPT plugin.",
            project="CommerceGov",
            category="research",
            topics=["ChatGPT plugin", "Claude"],
            entities=[{"name": "Nikolay Galinov", "type": "person"}],
            event_date="2026-07-01",
            importance=0.7,
        )
    )
    ingestor = make_ingestor(tmp_path, chat_fn=chat)
    batch = ingestor.ingest_batch(
        [UploadedFile("findings.md", b"# Findings\nClaude reviewed the ChatGPT plugin with Nikolay Galinov.")]
    )
    assert batch.status == "ok"
    result = batch.results[0]
    assert result.status == IngestStatus.INGESTED
    item = _item(ingestor, result.item_id)
    assert item["normalized_text"].startswith("# Findings")
    assert item["summary"] == "Claude findings about the ChatGPT plugin."
    assert item["project"] == "CommerceGov" and item["project_source"] == "llm"
    assert item["event_date"] == "2026-07-01" and item["event_date_source"] == "llm_inferred"
    assert item["captured_at"][:10] != "" and item["captured_at"] != item["event_date"]
    assert sorted(item["topics"]) == ["ChatGPT plugin", "Claude"]
    assert item["entities"][0]["name"] == "Nikolay Galinov"
    assert item["ingest_command_id"] == batch.command_id
    assert (ingestor.file_store.root / item["source_path"]).read_bytes().startswith(b"# Findings")


def test_operator_project_overrides_model_project(tmp_path):
    ingestor = make_ingestor(tmp_path, chat_fn=scripted_chat(lambda _u: classification_json(project="Other")))
    batch = ingestor.ingest_batch([UploadedFile("a.txt", b"some text")], project="CommerceGov")
    item = _item(ingestor, batch.results[0].item_id)
    assert item["project"] == "CommerceGov" and item["project_source"] == "operator"


def test_topics_are_relational_and_shared_across_items(tmp_path):
    chat = scripted_chat(lambda u: classification_json(topics=["OAuth", "Security"] if "one" in u else ["oauth"]))
    ingestor = make_ingestor(tmp_path, chat_fn=chat)
    ingestor.ingest_batch([UploadedFile("1.txt", b"doc one"), UploadedFile("2.txt", b"doc two")])
    with db.get_conn(ingestor.database_path) as conn:
        topics = [r[0] for r in conn.execute("SELECT normalized_name FROM knowledge_topics ORDER BY 1")]
        links = conn.execute("SELECT COUNT(*) FROM knowledge_item_topics").fetchone()[0]
    assert topics == ["oauth", "security"]
    assert links == 3


def test_classification_failure_keeps_extracted_text(tmp_path):
    ingestor = make_ingestor(tmp_path, chat_fn=scripted_chat(lambda _u: "garbage {"))
    batch = ingestor.ingest_batch([UploadedFile("notes.txt", b"Important deterministic content")])
    result = batch.results[0]
    assert result.status == IngestStatus.INGESTED
    assert result.classification_status == ClassificationStatus.INVALID_OUTPUT
    item = _item(ingestor, result.item_id)
    assert item["normalized_text"] == "Important deterministic content"
    assert item["summary"] is None and item["topics"] == []
    assert item["status"] == "unclassified"


def test_duplicate_upload_does_not_create_second_item_or_file(tmp_path):
    ingestor = make_ingestor(tmp_path)
    first = ingestor.ingest_batch([UploadedFile("a.txt", b"same bytes")])
    second = ingestor.ingest_batch([UploadedFile("renamed.txt", b"same bytes")])
    dup = second.results[0]
    assert dup.status == IngestStatus.DUPLICATE
    assert dup.existing_item_id == first.results[0].item_id
    assert "already stored" in dup.message
    with db.get_conn(ingestor.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM knowledge_items").fetchone()[0] == 1
        row = conn.execute("SELECT original_filename FROM knowledge_items").fetchone()
    assert row[0] == "a.txt"
    assert len([p for p in (tmp_path / "store").rglob("*") if p.is_file()]) == 1


def test_pdf_and_docx_ingestion(tmp_path):
    ingestor = make_ingestor(tmp_path)
    batch = ingestor.ingest_batch(
        [
            UploadedFile("bench.pdf", make_pdf(["benchmark 5000 products", "latency p95 120ms"])),
            UploadedFile("review.docx", make_docx(heading="Review", paragraphs=["OAuth CommerceGov notes"])),
        ]
    )
    assert [r.status for r in batch.results] == [IngestStatus.INGESTED, IngestStatus.INGESTED]
    assert _item(ingestor, batch.results[0].item_id)["content_kind"] == "document"
    assert "5000 products" in _item(ingestor, batch.results[0].item_id)["normalized_text"]
    assert "OAuth CommerceGov" in _item(ingestor, batch.results[1].item_id)["normalized_text"]


def test_scanned_pdf_is_stored_and_flagged_needs_vision(tmp_path):
    ingestor = make_ingestor(tmp_path)
    result = ingestor.ingest_batch([UploadedFile("scan.pdf", make_blank_pdf())]).results[0]
    assert result.status == IngestStatus.INGESTED
    assert result.extraction_status == ExtractionStatus.NEEDS_VISION
    assert result.classification_status == ClassificationStatus.SKIPPED_NO_TEXT
    assert _item(ingestor, result.item_id)["status"] == "needs_vision"


def test_image_without_vision_model_is_stored_without_fake_text(tmp_path):
    ingestor = make_ingestor(tmp_path)
    result = ingestor.ingest_batch([UploadedFile("shot.png", PNG_1X1)]).results[0]
    assert result.status == IngestStatus.INGESTED
    assert result.vision_status == VisionStatus.UNAVAILABLE
    assert "vision_model_not_configured" in result.warnings
    item = _item(ingestor, result.item_id)
    assert item["normalized_text"] is None and item["raw_text"] is None
    assert item["summary"] is None
    assert item["status"] == "needs_vision"


class _FakeVision:
    def __init__(self, outcome):
        self.outcome = outcome

    def describe_image(self, data, mime_type):
        return self.outcome


def test_text_screenshot_becomes_searchable_by_visible_text(tmp_path):
    vision = _FakeVision(
        VisionOutcome(
            status=VisionStatus.OK,
            model="vision-test",
            result=VisionResult(
                visible_text="OAuth consent screen for CommerceGov",
                description="Browser screenshot of a login dialog.",
                image_type="text_screenshot",
            ),
        )
    )
    ingestor = make_ingestor(tmp_path, vision=vision)
    result = ingestor.ingest_batch([UploadedFile("shot.png", PNG_1X1)]).results[0]
    item = _item(ingestor, result.item_id)
    assert item["normalized_text"].startswith("OAuth consent screen for CommerceGov")
    assert item["visual_description"] == "Browser screenshot of a login dialog."
    assert item["metadata"]["image_type"] == "text_screenshot"
    with db.get_conn(ingestor.database_path) as conn:
        hits = repo.search_items(conn, query="consent CommerceGov", filters=repo.KnowledgeSearchFilters(), limit=5)
    assert [h["id"] for h in hits["hits"]] == [result.item_id]


def test_photo_keeps_visual_context_first():
    text = vision_result_to_text(
        VisionResult(visible_text="EXIT", description="Whiteboard with a system diagram.", image_type="whiteboard")
    )
    assert text.startswith("[image: whiteboard] Whiteboard with a system diagram.")
    assert "EXIT" in text


def test_vision_failure_is_explicit(tmp_path):
    vision = _FakeVision(VisionOutcome(status=VisionStatus.FAILED, warning="vision_request_failed"))
    ingestor = make_ingestor(tmp_path, vision=vision)
    result = ingestor.ingest_batch([UploadedFile("shot.jpg", JPEG_HEADER)]).results[0]
    assert result.status == IngestStatus.INGESTED
    assert result.vision_status == VisionStatus.FAILED
    assert _item(ingestor, result.item_id)["normalized_text"] is None


def test_batch_failure_isolation(tmp_path, monkeypatch):
    def chat(messages, **_k):
        if "EXPLODE" in messages[-1]["content"]:
            raise RuntimeError("model crashed")
        return classification_json(topics=["ok"])

    ingestor = make_ingestor(tmp_path, chat_fn=chat)
    original_refresh = repo.refresh_fts

    def flaky_refresh(conn, item_id):
        row = conn.execute("SELECT original_filename FROM knowledge_items WHERE id = ?", (item_id,)).fetchone()
        if row and row[0] == "dbfail.txt":
            raise RuntimeError("simulated persistence failure")
        return original_refresh(conn, item_id)

    monkeypatch.setattr(repo, "refresh_fts", flaky_refresh)
    files = [UploadedFile(f"good{i}.txt", f"good document {i}".encode()) for i in range(3)]
    files += [
        UploadedFile("tool.exe", b"MZ\x00\x00\x00binary"),
        UploadedFile("classify.txt", b"EXPLODE please"),
        UploadedFile("dbfail.txt", b"this one fails in the transaction"),
        UploadedFile("empty.txt", b""),
    ]
    batch = ingestor.ingest_batch(files)
    statuses = {r.filename: r.status for r in batch.results}
    assert statuses["good0.txt"] == statuses["good1.txt"] == statuses["good2.txt"] == IngestStatus.INGESTED
    assert statuses["tool.exe"] == IngestStatus.UNSUPPORTED
    assert statuses["classify.txt"] == IngestStatus.INGESTED
    assert statuses["dbfail.txt"] == IngestStatus.FAILED
    assert statuses["empty.txt"] == IngestStatus.REJECTED
    assert batch.status == "partial"
    assert batch.counts == {"ingested": 4, "duplicate": 0, "unsupported": 1, "rejected": 1, "failed": 1}

    classify_result = next(r for r in batch.results if r.filename == "classify.txt")
    assert classify_result.classification_status == ClassificationStatus.UNAVAILABLE
    assert _item(ingestor, classify_result.item_id)["normalized_text"] == "EXPLODE please"

    with db.get_conn(ingestor.database_path) as conn:
        names = {r[0] for r in conn.execute("SELECT original_filename FROM knowledge_items")}
    assert "dbfail.txt" not in names
    assert len(names) == 4
    # The failed file's original was removed; the four committed originals remain.
    assert len([p for p in (tmp_path / "store").rglob("*") if p.is_file()]) == 4


def test_oversize_file_rejected(tmp_path):
    ingestor = make_ingestor(tmp_path, max_bytes=10)
    result = ingestor.ingest_batch([UploadedFile("big.txt", b"x" * 11)]).results[0]
    assert result.status == IngestStatus.REJECTED
    assert result.error_code == "file_too_large"


def test_ingest_batch_writes_command_log_without_raw_text(tmp_path):
    ingestor = make_ingestor(tmp_path)
    batch = ingestor.ingest_batch([UploadedFile("secret.txt", b"private contact data 555-1234")])
    with db.get_conn(ingestor.database_path) as conn:
        row = conn.execute("SELECT * FROM command_log WHERE id = ?", (batch.command_id,)).fetchone()
    assert row["status"] == "succeeded"
    assert row["tool_name"] == "knowledge_ingest"
    summary = json.loads(row["result_summary_json"])
    assert summary["counts"]["ingested"] == 1
    assert "555-1234" not in (row["result_summary_json"] + (row["tool_arguments_json"] or ""))


# --------------------------------------------------------------------- linking


def _seed_people(db_path: Path, names: list[str]) -> list[int]:
    lead, _ = db.upsert_lead({"company_name": "Galinov Labs"}, db_path=db_path)
    return [db.add_person(lead["id"], {"name": n}, db_path=db_path)["id"] for n in names]


def test_person_entity_links_only_on_unique_exact_match(tmp_path):
    chat = scripted_chat(
        lambda _u: classification_json(
            entities=[
                {"name": "nikolay  galinov", "type": "person"},
                {"name": "Jane Twin", "type": "person"},
                {"name": "Nikola", "type": "person"},
                {"name": "Nikolay Galinovski", "type": "person"},
                {"name": "Galinov Labs", "type": "organization"},
            ]
        )
    )
    ingestor = make_ingestor(tmp_path, chat_fn=chat)
    (person_id, _twin1, _twin2) = _seed_people(
        ingestor.database_path, ["Nikolay Galinov", "Jane Twin", "Jane Twin"]
    )
    result = ingestor.ingest_batch([UploadedFile("meeting.txt", b"met them")]).results[0]
    entities = {e["name"]: e for e in _item(ingestor, result.item_id)["entities"]}
    assert entities["nikolay galinov"]["link_status"] == "linked"
    assert entities["nikolay galinov"]["person_id"] == person_id
    assert entities["Jane Twin"]["link_status"] == "ambiguous"
    assert entities["Jane Twin"]["person_id"] is None
    assert entities["Nikola"]["link_status"] == "unlinked"
    assert entities["Nikolay Galinovski"]["link_status"] == "unlinked"
    assert entities["Galinov Labs"]["link_status"] == "linked"
    assert entities["Galinov Labs"]["lead_id"] is not None
    # No people or leads were created or merged by linking.
    with db.get_conn(ingestor.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM people").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0] == 1
