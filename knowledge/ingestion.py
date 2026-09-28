"""Knowledge ingestion orchestration.

Each file is processed independently: a failure in one file never rolls back
another. Per file, model calls (vision, classification) happen *before* the
database transaction opens, and the item plus its topics/entities/FTS row are
committed in one transaction. One command-log entry records the whole batch.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import db
from knowledge import repository as repo
from knowledge.classification import ChatFn, ClassificationOutcome, classify_text
from knowledge.extractors import ExtractionResult, extract_content, normalize_text
from knowledge.router import route_file
from knowledge.schemas import (
    BatchIngestResult,
    ClassificationStatus,
    ContentKind,
    ExtractionStatus,
    FileIngestResult,
    IngestStatus,
    VisionStatus,
)
from knowledge.storage import OriginalFileStore, sanitize_filename, sha256_hex
from knowledge.embeddings import EmbeddingRuntime
from knowledge.vision import UnavailableVisionProvider, VisionProvider, vision_result_to_text

logger = logging.getLogger(__name__)

MAX_FILES_PER_BATCH = 50


class KnowledgeRuntimeUnsupportedError(Exception):
    error_code = "knowledge_postgresql_runtime_unsupported"

    def __init__(self) -> None:
        self.message = (
            "Knowledge ingestion and search are supported on the SQLite runtime only. "
            "PostgreSQL knowledge persistence is not implemented."
        )
        super().__init__(self.message)


def require_knowledge_sqlite_runtime(store: Any) -> Path:
    if getattr(store, "backend", "sqlite") != "sqlite":
        raise KnowledgeRuntimeUnsupportedError()
    return Path(store.database_path)


@dataclass
class UploadedFile:
    filename: str
    data: bytes


@dataclass
class KnowledgeIngestor:
    database_path: Path
    file_store: OriginalFileStore
    max_bytes: int = 25 * 1024 * 1024
    vision: VisionProvider = field(default_factory=UnavailableVisionProvider)
    chat_fn: Optional[ChatFn] = None
    classify: bool = True
    model_name: str = "local-model"
    command_log_store: Any = None
    embeddings: Optional[EmbeddingRuntime] = None

    # ------------------------------------------------------------------ batch

    def ingest_batch(
        self,
        files: list[UploadedFile],
        *,
        project: Optional[str] = None,
    ) -> BatchIngestResult:
        # services must load before repositories.command_log_store (existing import cycle).
        from services.command_log import CommandStatus, transition
        from repositories.command_log_store import SqliteCommandLogStore

        command_log = self.command_log_store or SqliteCommandLogStore(self.database_path)
        entry = command_log.create(f"Knowledge ingest ({len(files)} file(s))")
        entry.intent = "knowledge_ingest"
        entry.tool_name = "knowledge_ingest"
        entry.risk_class = "write"
        entry.tool_arguments = {
            "file_count": len(files),
            "filenames": [sanitize_filename(f.filename) for f in files][:MAX_FILES_PER_BATCH],
            "project": project,
        }
        transition(entry, CommandStatus.PLANNED)
        command_log.update(entry)
        transition(entry, CommandStatus.EXECUTING)
        command_log.update(entry)

        results: list[FileIngestResult] = []
        if len(files) > MAX_FILES_PER_BATCH:
            for f in files[MAX_FILES_PER_BATCH:]:
                results.append(
                    FileIngestResult(
                        filename=sanitize_filename(f.filename),
                        status=IngestStatus.REJECTED,
                        error_code="batch_too_large",
                        message=f"Only {MAX_FILES_PER_BATCH} files are accepted per upload.",
                    )
                )
            files = files[:MAX_FILES_PER_BATCH]

        processed: list[FileIngestResult] = []
        for upload in files:
            try:
                processed.append(self.ingest_one(upload, project=project, command_id=str(entry.id)))
            except Exception:
                logger.exception("knowledge ingest failed for one file")
                processed.append(
                    FileIngestResult(
                        filename=sanitize_filename(upload.filename),
                        status=IngestStatus.FAILED,
                        error_code="ingest_failed",
                        message="Unexpected failure while ingesting this file. Nothing was stored for it.",
                    )
                )
        results = processed + results

        counts = {status.value: 0 for status in IngestStatus}
        for r in results:
            counts[r.status.value] += 1
        ok_like = counts["ingested"] + counts["duplicate"]
        if ok_like == len(results) and results:
            batch_status = "ok"
        elif ok_like:
            batch_status = "partial"
        else:
            batch_status = "error"

        transition(entry, CommandStatus.SUCCEEDED if ok_like or not results else CommandStatus.FAILED)
        entry.result_summary = {
            "counts": counts,
            "item_ids": [r.item_id for r in results if r.item_id],
            "duplicate_of": [r.existing_item_id for r in results if r.existing_item_id],
        }
        if batch_status == "error":
            entry.error_code = "knowledge_ingest_no_files_stored"
            entry.error_message = "No uploaded file was stored."
        command_log.update(entry)
        return BatchIngestResult(
            status=batch_status,
            command_id=str(entry.id),
            counts=counts,
            results=results,
        )

    # ------------------------------------------------------------------ single

    def ingest_one(
        self,
        upload: UploadedFile,
        *,
        project: Optional[str] = None,
        command_id: Optional[str] = None,
    ) -> FileIngestResult:
        filename = sanitize_filename(upload.filename)
        data = upload.data
        if not data:
            return FileIngestResult(
                filename=filename, status=IngestStatus.REJECTED, error_code="empty_file",
                message="The file is empty.",
            )
        if len(data) > self.max_bytes:
            return FileIngestResult(
                filename=filename, status=IngestStatus.REJECTED, error_code="file_too_large",
                message=f"File exceeds the {self.max_bytes // (1024 * 1024)} MB limit.",
            )

        content_hash = sha256_hex(data)
        with db.get_conn(self.database_path) as conn:
            existing = repo.find_item_by_hash(conn, content_hash)
        if existing:
            return self._duplicate_result(filename, content_hash, existing)

        route = route_file(filename, data)
        if route.kind == ContentKind.UNSUPPORTED:
            return FileIngestResult(
                filename=filename,
                status=IngestStatus.UNSUPPORTED,
                content_kind=ContentKind.UNSUPPORTED,
                content_hash=content_hash,
                error_code=route.reason,
                message="Unsupported file type. Supported: text/markdown/JSON/code, PDF, DOCX, PNG, JPEG, WEBP.",
            )

        warnings: list[str] = []
        metadata: dict[str, Any] = {"route_reason": route.reason}

        # 1. Deterministic extraction.
        if route.kind == ContentKind.IMAGE:
            extraction = ExtractionResult(status=ExtractionStatus.NEEDS_VISION, method="image")
        else:
            extraction = extract_content(data, route.mime_type)
        warnings.extend(extraction.warnings)
        metadata["extraction"] = extraction.metadata
        raw_text = extraction.raw_text or ""
        normalized = normalize_text(raw_text)

        # 2. Vision for images (PDF page rendering is not implemented; see docs).
        vision_status = VisionStatus.NOT_APPLICABLE
        vision_model: Optional[str] = None
        visual_description: Optional[str] = None
        if route.kind == ContentKind.IMAGE:
            outcome = self.vision.describe_image(data, route.mime_type)
            vision_status = outcome.status
            vision_model = outcome.model
            if outcome.warning:
                warnings.append(outcome.warning)
            if outcome.status == VisionStatus.OK and outcome.result is not None:
                raw_text = outcome.result.visible_text
                visual_description = outcome.result.description
                normalized = normalize_text(vision_result_to_text(outcome.result))
                metadata["image_type"] = outcome.result.image_type
        elif extraction.status == ExtractionStatus.NEEDS_VISION:
            vision_status = VisionStatus.UNAVAILABLE
            warnings.append("pdf_page_vision_not_implemented")

        # 3. Advisory classification (never blocks persistence).
        if not self.classify:
            classification = ClassificationOutcome(status=ClassificationStatus.DISABLED)
        else:
            classification = classify_text(
                normalized, filename=filename, chat_fn=self.chat_fn, model_name=self.model_name
            )
        if classification.warning:
            warnings.append(classification.warning)
        cls = classification.classification

        operator_project = (project or "").strip()[:120] or None
        project_value = operator_project or (cls.project if cls else None)
        project_source = "operator" if operator_project else ("llm" if cls and cls.project else None)

        captured_at = datetime.now(timezone.utc).isoformat()
        values = {
            "content_hash": content_hash,
            "source_path": "",  # filled after the original is written
            "original_filename": filename,
            "mime_type": route.mime_type,
            "content_kind": route.kind.value,
            "size_bytes": len(data),
            "raw_text": raw_text or None,
            "normalized_text": normalized or None,
            "extraction_method": extraction.method,
            "extraction_status": extraction.status.value,
            "vision_status": vision_status.value,
            "vision_model": vision_model,
            "visual_description": visual_description,
            "summary": cls.summary if cls else None,
            "project": project_value,
            "project_source": project_source,
            "category": cls.category if cls else None,
            "sub_category": cls.sub_category if cls else None,
            "event_date": cls.event_date.isoformat() if cls and cls.event_date else None,
            "event_date_source": "llm_inferred" if cls and cls.event_date else None,
            "captured_at": captured_at,
            "importance": cls.importance if cls else None,
            "classification_status": classification.status.value,
            "classification_model": classification.model,
            "classification_warning": classification.warning,
            "metadata_json": {**metadata, "warnings": warnings},
            "ingest_command_id": command_id,
        }

        # 4. Preserve original, then commit metadata in one transaction.
        stored = self.file_store.save(data, content_hash=content_hash, extension=route.extension)
        values["source_path"] = stored.relative_path
        try:
            with db.get_conn(self.database_path) as conn:
                item_id = repo.insert_item(conn, values)
                if cls is not None:
                    repo.attach_classification(conn, item_id, cls)
                repo.refresh_fts(conn, item_id)
                item = repo.get_item(conn, item_id, include_text=False)
        except sqlite3.IntegrityError:
            # Concurrent upload of identical content won the race.
            self.file_store.discard_if_created(stored)
            with db.get_conn(self.database_path) as conn:
                existing = repo.find_item_by_hash(conn, content_hash)
            if existing:
                return self._duplicate_result(filename, content_hash, existing)
            raise
        except Exception:
            self.file_store.discard_if_created(stored)
            raise

        # 5. Phase K1: embed the new item's chunks after commit. Never fails ingestion.
        embedding = self._embed_new_item(item_id, warnings)

        return FileIngestResult(
            filename=filename,
            status=IngestStatus.INGESTED,
            embedding=embedding,
            item_id=item_id,
            content_kind=route.kind,
            mime_type=route.mime_type,
            content_hash=content_hash,
            extraction_status=extraction.status,
            vision_status=vision_status,
            classification_status=classification.status,
            warnings=warnings,
            preview=_preview(item, normalized),
        )

    def _embed_new_item(self, item_id: int, warnings: list[str]) -> Optional[dict[str, Any]]:
        runtime = self.embeddings
        if runtime is None or not runtime.enabled or not runtime.settings.embed_on_ingest:
            return None
        from knowledge.embedding_index import index_knowledge

        try:
            report = index_knowledge(self.database_path, runtime, item_ids=[item_id])
        except Exception:
            logger.exception("embedding after ingest failed")
            warnings.append("embedding_index_error")
            return {"status": "failed", "error_codes": {"embedding_index_error": 1}}
        if report.failed:
            warnings.append("embedding_failed:" + ",".join(sorted(report.error_codes)))
        return {
            "status": report.status,
            "model": report.model,
            "chunks": report.chunks_total,
            "indexed": report.indexed,
            "failed": report.failed,
            "error_codes": report.error_codes,
        }

    @staticmethod
    def _duplicate_result(filename: str, content_hash: str, existing: dict[str, Any]) -> FileIngestResult:
        return FileIngestResult(
            filename=filename,
            status=IngestStatus.DUPLICATE,
            existing_item_id=int(existing["id"]),
            content_kind=ContentKind(existing["content_kind"]),
            mime_type=existing["mime_type"],
            content_hash=content_hash,
            message=(
                f"Identical file already stored as knowledge item {existing['id']} "
                f"('{existing['original_filename']}'). Nothing new was created."
            ),
        )


def _preview(item: Optional[dict[str, Any]], normalized: str) -> dict[str, Any]:
    item = item or {}
    return {
        "summary": item.get("summary"),
        "project": item.get("project"),
        "category": item.get("category"),
        "sub_category": item.get("sub_category"),
        "event_date": item.get("event_date"),
        "topics": item.get("topics", []),
        "entities": [e["name"] for e in item.get("entities", [])],
        "status": item.get("status"),
        "text_excerpt": normalized[:300],
        "text_chars": len(normalized),
    }


def build_ingestor(store: Any, cfg: Any, **overrides: Any) -> KnowledgeIngestor:
    from llm import LM_ENDPOINT
    from knowledge.embeddings import get_embedding_runtime
    from knowledge.vision import build_vision_provider

    database_path = require_knowledge_sqlite_runtime(store)
    kwargs: dict[str, Any] = {
        "database_path": database_path,
        "file_store": OriginalFileStore(cfg.knowledge_storage_dir),
        "max_bytes": cfg.knowledge_max_upload_bytes,
        "vision": build_vision_provider(
            endpoint=LM_ENDPOINT,
            model=cfg.knowledge_vision_model,
            timeout=max(cfg.lmstudio_timeout, 60.0),
        ),
        "classify": cfg.knowledge_classify,
        "model_name": cfg.lmstudio_model,
        "embeddings": get_embedding_runtime(store),
    }
    kwargs.update(overrides)
    return KnowledgeIngestor(**kwargs)
