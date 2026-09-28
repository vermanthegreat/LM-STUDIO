"""Deterministic in-memory fixtures for knowledge tests (no external files)."""

from __future__ import annotations

import json
import zipfile
from io import BytesIO
from pathlib import Path
from typing import Any, Callable, Optional

from knowledge.ingestion import KnowledgeIngestor
from knowledge.storage import OriginalFileStore
from knowledge.vision import UnavailableVisionProvider
from repositories.sqlite_store import SqliteContactStore

PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082"
)
JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" + b"\x00" * 32
WEBP_HEADER = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"\x00" * 24


def make_pdf(lines: list[str]) -> bytes:
    """Build a minimal valid single-page PDF with a Helvetica text layer."""
    ops = ["BT", "/F1 12 Tf", "72 720 Td"]
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        ops.append(f"({escaped}) Tj")
        ops.append("0 -16 Td")
    ops.append("ET")
    content = "\n".join(ops)
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n{obj}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def make_blank_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


_W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'


def _p(text: str, style: Optional[str] = None, numbered: bool = False) -> str:
    ppr = ""
    if style or numbered:
        inner = f'<w:pStyle w:val="{style}"/>' if style else ""
        if numbered:
            inner += '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
        ppr = f"<w:pPr>{inner}</w:pPr>"
    return f'<w:p>{ppr}<w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'


def make_docx(
    *,
    heading: str,
    paragraphs: list[str],
    bullets: list[str] = (),
    table: list[list[str]] = (),
) -> bytes:
    body = _p(heading, style="Heading1")
    body += "".join(_p(t) for t in paragraphs)
    body += "".join(_p(t, numbered=True) for t in bullets)
    if table:
        rows = "".join(
            "<w:tr>" + "".join(f"<w:tc>{_p(cell)}</w:tc>" for cell in row) + "</w:tr>" for row in table
        )
        body += f"<w:tbl>{rows}</w:tbl>"
    document = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document {_W}><w:body>{body}</w:body></w:document>'
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.'
            'wordprocessingml.document.main+xml"/></Types>',
        )
        zf.writestr("word/document.xml", document)
    return buf.getvalue()


def classification_json(**overrides: Any) -> str:
    payload = {
        "summary": "Test summary.",
        "project": None,
        "category": "reference",
        "sub_category": None,
        "topics": [],
        "entities": [],
        "event_date": None,
        "importance": 0.3,
    }
    payload.update(overrides)
    return json.dumps(payload)


def scripted_chat(mapping: Callable[[str], Optional[str]]):
    """chat_fn whose reply depends on the user prompt (document text)."""
    calls: list[str] = []

    def chat(messages, **_kwargs):
        user = messages[-1]["content"]
        calls.append(user)
        return mapping(user)

    chat.calls = calls  # type: ignore[attr-defined]
    return chat


def make_ingestor(tmp_path: Path, **overrides: Any) -> KnowledgeIngestor:
    import services  # noqa: F401  (must load before repositories.command_log_store)
    from repositories.command_log_store import SqliteCommandLogStore

    db_path = tmp_path / "knowledge.db"
    SqliteContactStore(db_path).init_db()
    kwargs: dict[str, Any] = {
        "database_path": db_path,
        "file_store": OriginalFileStore(tmp_path / "store"),
        "vision": UnavailableVisionProvider(),
        "chat_fn": scripted_chat(lambda _u: None),
        "command_log_store": SqliteCommandLogStore(db_path),
    }
    kwargs.update(overrides)
    return KnowledgeIngestor(**kwargs)
