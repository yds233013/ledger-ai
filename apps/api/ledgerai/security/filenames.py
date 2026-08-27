"""Filename sanitization.

Rule: an uploaded filename is display data. It never participates in building
a filesystem path or an object-storage key. Storage keys are generated from a
UUID; the original name is stored in a column and echoed back to the UI only.
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from pathlib import PurePosixPath

_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_DOTS = re.compile(r"\.{2,}")
MAX_STEM = 80


def _clean(part: str) -> str:
    part = unicodedata.normalize("NFKD", part)
    part = part.encode("ascii", "ignore").decode("ascii").lower()
    part = _UNSAFE.sub("-", part)
    return _DOTS.sub(".", part).strip("-.")


def sanitize_filename(raw: str) -> str:
    """Return a safe, path-free display/storage filename.

    Strips directory components (including Windows separators and traversal),
    normalizes unicode, and collapses anything outside [a-z0-9._-]. The stem
    and the extension are cleaned separately so a name whose stem is entirely
    non-ASCII still keeps a usable extension.
    """
    # Neutralize both separator styles before taking the basename.
    basename = PurePosixPath(raw.replace("\\", "/")).name
    stem, _, suffix = basename.rpartition(".")
    if not stem:  # no dot at all -> the whole thing is the stem
        stem, suffix = basename, ""

    clean_stem = _clean(stem)[:MAX_STEM] or "upload"
    clean_suffix = _clean(suffix)[:12]
    return f"{clean_stem}.{clean_suffix}" if clean_suffix else clean_stem


def build_storage_key(user_id: uuid.UUID | str, safe_filename: str) -> str:
    """Generate an unguessable, collision-free object key.

    The user's filename contributes nothing to the path structure.
    """
    return f"users/{user_id}/uploads/{uuid.uuid4()}/{safe_filename}"
