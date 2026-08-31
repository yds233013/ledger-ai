"""Reading a transaction table out of a bank statement PDF.

Text layer only, by design. A statement produced by a bank is a generated
document: the exact characters and their positions are already in the file, and
rasterising them at 200 DPI to run OCR over the result throws that away and
reintroduces errors the original never had. A scanned statement is refused with
a message pointing at CSV export rather than guessed at.

Receipts are untouched by all of this. They keep their OCR path and their
five-page cap; nothing here runs for `UploadKind.IMAGE`.
"""

from .extract import Word, extract_pages
from .layout import Line, group_lines, infer_columns
from .parse import ParsedRow, ParsedStatement, parse_statement
from .staging import (
    dedupe_hash_for,
    expiry_from,
    logical_key,
    mark_committed,
    purge_original,
    rows_for_commit,
    stage,
)
from .verify import VerificationResult, verify_text_layer

__all__ = [
    "Line",
    "ParsedRow",
    "ParsedStatement",
    "VerificationResult",
    "Word",
    "dedupe_hash_for",
    "expiry_from",
    "extract_pages",
    "group_lines",
    "infer_columns",
    "logical_key",
    "mark_committed",
    "parse_statement",
    "purge_original",
    "rows_for_commit",
    "stage",
    "verify_text_layer",
]
