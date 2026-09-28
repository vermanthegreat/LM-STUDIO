"""Deterministic text extraction for text, PDF, and DOCX content."""

from __future__ import annotations

import json
import re
import unicodedata
import zipfile
from dataclasses import dataclass, field
from io import BytesIO
from xml.etree import ElementTree

from knowledge.router import DOCX_MIME, PDF_MIME
from knowledge.schemas import ExtractionStatus

# A PDF whose extracted text has fewer meaningful characters per page than this
# is treated as scanned/visual and flagged as requiring vision processing.
MIN_PDF_CHARS_PER_PAGE = 25
MAX_TEXT_CHARS = 2_000_000

_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTI_BLANK_RE = re.compile(r"\n{3,}")
_TRAILING_WS_RE = re.compile(r"[ \t]+\n")


@dataclass
class ExtractionResult:
    status: ExtractionStatus
    method: str
    raw_text: str = ""
    warnings: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFC", text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CONTROL_RE.sub("", text)
    text = _TRAILING_WS_RE.sub("\n", text)
    text = _MULTI_BLANK_RE.sub("\n\n", text)
    return text.strip()[:MAX_TEXT_CHARS]


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode bytes safely. Returns (text, encoding_used)."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        return data.decode("latin-1", errors="replace"), "latin-1"


def extract_plain_text(data: bytes, mime_type: str) -> ExtractionResult:
    text, encoding = decode_text(data)
    meta: dict = {"encoding": encoding}
    warnings: list[str] = []
    if mime_type == "application/json":
        try:
            parsed = json.loads(text)
            text = json.dumps(parsed, ensure_ascii=False, indent=2)
            meta["json_valid"] = True
        except json.JSONDecodeError:
            meta["json_valid"] = False
            warnings.append("json_invalid_kept_as_text")
    status = ExtractionStatus.OK if text.strip() else ExtractionStatus.EMPTY
    return ExtractionResult(status=status, method="text_decode", raw_text=text, warnings=warnings, metadata=meta)


def extract_pdf(data: bytes) -> ExtractionResult:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover - dependency declared in requirements
        return ExtractionResult(status=ExtractionStatus.FAILED, method="pypdf", warnings=["pypdf_not_installed"])

    try:
        reader = PdfReader(BytesIO(data))
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception:
                return ExtractionResult(status=ExtractionStatus.FAILED, method="pypdf", warnings=["pdf_encrypted"])
        pages: list[str] = []
        for index, page in enumerate(reader.pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception:
                page_text = ""
            if page_text.strip():
                pages.append(f"[page {index}]\n{page_text.strip()}")
        page_count = len(reader.pages)
    except Exception as exc:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            method="pypdf",
            warnings=[f"pdf_parse_error:{type(exc).__name__}"],
        )

    text = "\n\n".join(pages)
    meaningful = len(re.sub(r"\s|\[page \d+\]", "", text))
    meta = {"page_count": page_count, "pages_with_text": len(pages)}
    if page_count and meaningful < MIN_PDF_CHARS_PER_PAGE * page_count:
        return ExtractionResult(
            status=ExtractionStatus.NEEDS_VISION,
            method="pypdf",
            raw_text=text,
            warnings=["pdf_little_or_no_text_layer"],
            metadata=meta,
        )
    return ExtractionResult(status=ExtractionStatus.OK, method="pypdf", raw_text=text, metadata=meta)


_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_W = f"{{{_W_NS}}}"


def _docx_paragraph_text(p: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in p.iter():
        if node.tag == f"{_W}t" and node.text:
            parts.append(node.text)
        elif node.tag == f"{_W}tab":
            parts.append("\t")
        elif node.tag in (f"{_W}br", f"{_W}cr"):
            parts.append("\n")
    return "".join(parts)


def _docx_heading_level(p: ElementTree.Element) -> int | None:
    style = p.find(f"{_W}pPr/{_W}pStyle")
    if style is None:
        return None
    value = (style.get(f"{_W}val") or "").lower()
    match = re.match(r"heading\s*(\d)", value)
    if match:
        return int(match.group(1))
    if value == "title":
        return 1
    return None


def _docx_is_list_item(p: ElementTree.Element) -> bool:
    return p.find(f"{_W}pPr/{_W}numPr") is not None


def extract_docx(data: bytes) -> ExtractionResult:
    try:
        with zipfile.ZipFile(BytesIO(data)) as zf:
            xml_bytes = zf.read("word/document.xml")
        root = ElementTree.fromstring(xml_bytes)
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            method="docx_xml",
            warnings=[f"docx_parse_error:{type(exc).__name__}"],
        )

    body = root.find(f"{_W}body")
    blocks: list[str] = []
    tables = 0
    if body is not None:
        for child in body:
            if child.tag == f"{_W}p":
                text = _docx_paragraph_text(child).strip()
                if not text:
                    continue
                level = _docx_heading_level(child)
                if level:
                    blocks.append(f"{'#' * min(level, 6)} {text}")
                elif _docx_is_list_item(child):
                    blocks.append(f"- {text}")
                else:
                    blocks.append(text)
            elif child.tag == f"{_W}tbl":
                tables += 1
                rows: list[str] = []
                for tr in child.iter(f"{_W}tr"):
                    cells = []
                    for tc in tr.findall(f"{_W}tc"):
                        cell_text = " ".join(
                            _docx_paragraph_text(p).strip() for p in tc.iter(f"{_W}p")
                        ).strip()
                        cells.append(cell_text)
                    rows.append(" | ".join(cells))
                if rows:
                    blocks.append("\n".join(rows))
    text = "\n\n".join(blocks)
    status = ExtractionStatus.OK if text.strip() else ExtractionStatus.EMPTY
    return ExtractionResult(
        status=status,
        method="docx_xml",
        raw_text=text,
        metadata={"paragraph_blocks": len(blocks), "tables": tables},
    )


def extract_content(data: bytes, mime_type: str) -> ExtractionResult:
    if mime_type == PDF_MIME:
        return extract_pdf(data)
    if mime_type == DOCX_MIME:
        return extract_docx(data)
    return extract_plain_text(data, mime_type)
