"""Deterministic file-type routing. No model is consulted here."""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from io import BytesIO

from knowledge.schemas import ContentKind
from knowledge.storage import safe_extension

# Plain-text family: extension -> MIME type.
TEXT_EXTENSIONS: dict[str, str] = {
    "txt": "text/plain",
    "text": "text/plain",
    "log": "text/plain",
    "md": "text/markdown",
    "markdown": "text/markdown",
    "rst": "text/x-rst",
    "json": "application/json",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "toml": "application/toml",
    "ini": "text/plain",
    "cfg": "text/plain",
    "xml": "application/xml",
    "html": "text/html",
    "htm": "text/html",
    "css": "text/css",
    "py": "text/x-python",
    "js": "text/javascript",
    "mjs": "text/javascript",
    "ts": "text/x-typescript",
    "tsx": "text/x-typescript",
    "jsx": "text/javascript",
    "java": "text/x-java",
    "kt": "text/x-kotlin",
    "go": "text/x-go",
    "rs": "text/x-rust",
    "rb": "text/x-ruby",
    "php": "text/x-php",
    "c": "text/x-c",
    "h": "text/x-c",
    "cpp": "text/x-c++",
    "hpp": "text/x-c++",
    "cs": "text/x-csharp",
    "swift": "text/x-swift",
    "sh": "text/x-shellscript",
    "sql": "text/x-sql",
    "liquid": "text/plain",
}

IMAGE_SIGNATURES: tuple[tuple[str, str], ...] = (
    ("png", "image/png"),
    ("jpeg", "image/jpeg"),
    ("webp", "image/webp"),
)

PDF_MIME = "application/pdf"
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@dataclass(frozen=True)
class RouteDecision:
    kind: ContentKind
    mime_type: str
    extension: str
    reason: str


def _sniff_image(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _is_docx(data: bytes) -> bool:
    if not data.startswith(b"PK"):
        return False
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            return "word/document.xml" in zf.namelist()
    except zipfile.BadZipFile:
        return False


def _looks_textual(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample:
        # UTF-16 with BOM is still text.
        return sample.startswith((b"\xff\xfe", b"\xfe\xff"))
    try:
        sample.decode("utf-8")
        return True
    except UnicodeDecodeError as exc:
        # A multi-byte char split at the sample boundary is fine.
        return exc.start >= len(sample) - 4


def route_file(filename: str, data: bytes) -> RouteDecision:
    """Classify by magic bytes first, then by extension with a content check."""
    ext = safe_extension(filename)

    image_mime = _sniff_image(data)
    if image_mime:
        return RouteDecision(ContentKind.IMAGE, image_mime, ext, "magic_bytes")

    if data.startswith(b"%PDF-"):
        return RouteDecision(ContentKind.DOCUMENT, PDF_MIME, ext or "pdf", "magic_bytes")

    if _is_docx(data):
        return RouteDecision(ContentKind.DOCUMENT, DOCX_MIME, ext or "docx", "zip_structure")

    if ext in {"png", "jpg", "jpeg", "webp", "pdf", "docx"}:
        return RouteDecision(ContentKind.UNSUPPORTED, "application/octet-stream", ext, "extension_content_mismatch")

    if ext in TEXT_EXTENSIONS and _looks_textual(data):
        return RouteDecision(ContentKind.TEXT, TEXT_EXTENSIONS[ext], ext, "extension")

    if not ext and data and _looks_textual(data):
        return RouteDecision(ContentKind.TEXT, "text/plain", ext, "content_sniff")

    return RouteDecision(ContentKind.UNSUPPORTED, "application/octet-stream", ext, "unsupported_type")
