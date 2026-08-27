"""Upload validation: type, size, and structure.

Size is enforced by counting bytes as they stream in, not by trusting the
client-supplied Content-Length header.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

import filetype

from ..config import settings
from ..models import UploadKind

ALLOWED_CSV_EXTENSIONS = {".csv", ".txt"}
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
ALLOWED_IMAGE_MIME = {"image/png", "image/jpeg", "image/webp"}
ALLOWED_RECEIPT_MIME = ALLOWED_IMAGE_MIME | {"application/pdf"}

# Responses for stored receipts use this fixed allow-list rather than echoing
# whatever content type the upload claimed.
SAFE_RESPONSE_CONTENT_TYPES = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/webp": "image/webp",
    "application/pdf": "application/pdf",
}

# The CSV must expose a date, a description and an amount. Everything else is
# optional. Header matching is case/space/underscore-insensitive.
REQUIRED_FIELD_GROUPS: dict[str, set[str]] = {
    "date": {"date", "posteddate", "transactiondate", "postingdate", "posted"},
    "description": {"description", "details", "memo", "narrative", "name", "payee"},
    "amount": {"amount", "value", "debit", "credit", "amountusd"},
}

MAX_CSV_ROWS = 20_000


class ValidationError(Exception):
    """User-facing upload validation failure."""


@dataclass(slots=True)
class ValidatedUpload:
    kind: UploadKind
    content_type: str
    size_bytes: int


def normalize_header(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def detect_kind(filename: str, data: bytes) -> tuple[UploadKind, str]:
    """Classify by sniffed content first, extension second."""
    kind_guess = filetype.guess(data)

    if kind_guess is not None and kind_guess.mime == "application/pdf":
        return UploadKind.IMAGE, "application/pdf"

    if kind_guess is not None and kind_guess.mime.startswith("image/"):
        if kind_guess.mime not in ALLOWED_IMAGE_MIME:
            raise ValidationError(f"Unsupported image type: {kind_guess.mime}")
        return UploadKind.IMAGE, kind_guess.mime

    lowered = filename.lower()
    if any(lowered.endswith(ext) for ext in ALLOWED_IMAGE_EXTENSIONS):
        # Extension claims an image but the bytes are not one.
        raise ValidationError("File extension says image but the content is not a valid image")
    if lowered.endswith(".pdf"):
        raise ValidationError("File extension says PDF but the content is not a valid PDF")

    if any(lowered.endswith(ext) for ext in ALLOWED_CSV_EXTENSIONS):
        return UploadKind.CSV, "text/csv"

    raise ValidationError(
        "Only .csv statements and .png/.jpg/.webp/.pdf receipts are accepted"
    )


def safe_response_content_type(stored_content_type: str) -> str:
    """Never echo an upload's claimed content type back to a browser."""
    return SAFE_RESPONSE_CONTENT_TYPES.get(stored_content_type, "application/octet-stream")


def validate_size(size_bytes: int) -> None:
    if size_bytes <= 0:
        raise ValidationError("File is empty")
    if size_bytes > settings.max_upload_bytes:
        limit_mb = settings.max_upload_bytes / 1024 / 1024
        raise ValidationError(f"File exceeds the {limit_mb:.0f} MB limit")


def decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValidationError("CSV could not be decoded as UTF-8 or Latin-1")


def validate_csv_structure(data: bytes) -> dict[str, str]:
    """Validate headers and return a mapping of logical field -> actual header.

    Raises ValidationError with a message the UI can show verbatim.
    """
    text = decode_csv(data)
    if not text.strip():
        raise ValidationError("CSV is empty")

    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel  # type: ignore[assignment]

    reader = csv.reader(io.StringIO(text), dialect)
    try:
        header = next(reader)
    except StopIteration as exc:
        raise ValidationError("CSV has no header row") from exc

    normalized = {normalize_header(col): col for col in header if col.strip()}
    if not normalized:
        raise ValidationError("CSV header row is blank")

    mapping: dict[str, str] = {}
    missing: list[str] = []
    for logical, aliases in REQUIRED_FIELD_GROUPS.items():
        match = next((normalized[a] for a in aliases if a in normalized), None)
        if match is None:
            missing.append(logical)
        else:
            mapping[logical] = match

    if missing:
        raise ValidationError(
            "CSV is missing required column(s): "
            + ", ".join(missing)
            + ". Expected a date column, a description column and an amount column."
        )

    row_count = sum(1 for _ in reader)
    if row_count == 0:
        raise ValidationError("CSV contains a header but no data rows")
    if row_count > MAX_CSV_ROWS:
        raise ValidationError(f"CSV has {row_count} rows; the limit is {MAX_CSV_ROWS}")

    return mapping
