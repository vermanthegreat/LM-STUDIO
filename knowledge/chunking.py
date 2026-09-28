"""Deterministic chunking of extracted knowledge text (Phase K1).

Chunks are the unit of embedding. Boundaries depend only on the text and the
constants below, so re-chunking unchanged text yields identical chunks and
fingerprints; this is what lets indexing skip unchanged chunks.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

CHUNKER_VERSION = "k1-para-v1"
TARGET_CHARS = 1000
MAX_CHARS = 1500

_PARA_RE = re.compile(r"\n\s*\n")


@dataclass(frozen=True)
class TextChunk:
    index: int
    char_start: int
    char_end: int
    text: str
    fingerprint: str


def chunk_fingerprint(text: str) -> str:
    return hashlib.sha256(f"{CHUNKER_VERSION}\n{text}".encode("utf-8")).hexdigest()


def _paragraph_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _PARA_RE.finditer(text):
        if match.start() > pos:
            spans.append((pos, match.start()))
        pos = match.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    return spans


def _split_long(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Hard-split an oversized span at whitespace near MAX_CHARS."""
    out: list[tuple[int, int]] = []
    while end - start > MAX_CHARS:
        cut = text.rfind(" ", start + TARGET_CHARS // 2, start + MAX_CHARS)
        if cut <= start:
            cut = start + MAX_CHARS
        out.append((start, cut))
        start = cut
        while start < end and text[start].isspace():
            start += 1
    if end > start:
        out.append((start, end))
    return out


def chunk_text(text: str) -> list[TextChunk]:
    text = text or ""
    spans: list[tuple[int, int]] = []
    for start, end in _paragraph_spans(text):
        spans.extend(_split_long(text, start, end))

    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and end - merged[-1][0] <= TARGET_CHARS:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))

    chunks: list[TextChunk] = []
    for start, end in merged:
        body = text[start:end].strip()
        if not body:
            continue
        chunks.append(
            TextChunk(
                index=len(chunks),
                char_start=start,
                char_end=end,
                text=body,
                fingerprint=chunk_fingerprint(body),
            )
        )
    return chunks
