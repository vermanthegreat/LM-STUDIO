"""Content-addressed preservation of original uploaded files.

Originals are stored as ``<root>/originals/<sha[:2]>/<sha256><.ext>``. The
user-supplied filename never becomes a path component; it is kept (sanitized)
only as metadata. Identical content maps to the same path, so exact duplicates
never create a second copy.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

_UNSAFE_CHARS_RE = re.compile(r"[\x00-\x1f\x7f<>:\"/\\|?*]")
_EXT_RE = re.compile(r"^[a-z0-9]{1,10}$")
MAX_FILENAME_LEN = 200


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sanitize_filename(name: str | None) -> str:
    """Return a display-safe base filename with no directory components."""
    raw = str(name or "")
    # Strip any directory part using both POSIX and Windows semantics.
    base = PureWindowsPath(PurePosixPath(raw).name).name
    base = unicodedata.normalize("NFC", base)
    base = _UNSAFE_CHARS_RE.sub("_", base).strip().strip(".")
    if base in {"", ".", ".."}:
        base = "upload"
    if len(base) > MAX_FILENAME_LEN:
        stem, dot, ext = base.rpartition(".")
        if dot and _EXT_RE.match(ext.lower()):
            base = stem[: MAX_FILENAME_LEN - len(ext) - 1] + "." + ext
        else:
            base = base[:MAX_FILENAME_LEN]
    return base


def safe_extension(filename: str) -> str:
    """Lower-case extension (without dot) if it is short and alphanumeric, else ''."""
    _, dot, ext = sanitize_filename(filename).rpartition(".")
    ext = ext.lower()
    if dot and _EXT_RE.match(ext):
        return ext
    return ""


@dataclass(frozen=True)
class StoredOriginal:
    relative_path: str
    absolute_path: Path
    created: bool


class OriginalFileStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.originals_dir = self.root / "originals"

    def relative_path_for(self, content_hash: str, extension: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{64}", content_hash):
            raise ValueError("invalid content hash")
        suffix = f".{extension}" if extension and _EXT_RE.match(extension) else ""
        return f"originals/{content_hash[:2]}/{content_hash}{suffix}"

    def resolve(self, relative_path: str) -> Path:
        """Resolve a stored relative path, refusing anything outside the root."""
        candidate = (self.root / relative_path).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise ValueError("path escapes knowledge storage root")
        return candidate

    def save(self, data: bytes, *, content_hash: str, extension: str) -> StoredOriginal:
        if sha256_hex(data) != content_hash:
            raise ValueError("content hash mismatch")
        rel = self.relative_path_for(content_hash, extension)
        target = self.resolve(rel)
        if target.exists():
            return StoredOriginal(relative_path=rel, absolute_path=target, created=False)
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=".upload-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, target)
        except Exception:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise
        return StoredOriginal(relative_path=rel, absolute_path=target, created=True)

    def discard_if_created(self, stored: StoredOriginal) -> None:
        """Remove a file this request just created (used when the DB insert fails)."""
        if stored.created and stored.absolute_path.exists():
            stored.absolute_path.unlink()
