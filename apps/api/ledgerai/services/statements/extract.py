"""Positional text extraction, through the pypdfium2 already in the tree.

No new PDF parser. `camelot` shells out to Ghostscript — a perennial source of
remote-code-execution advisories — and every additional parser is more attack
surface pointed at the most sensitive file this product will ever hold.
pypdfium2 is already parsing these bytes in order to render receipt PDFs, so
using its text API adds capability without adding exposure.

What comes out is words with boxes. Everything downstream works on geometry:
which column a token sits in is the whole basis for reading a table that
carries no machine-readable structure at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import pypdfium2 as pdfium

from ...config import settings
from ...security.validators import ValidationError

logger = logging.getLogger(__name__)

# Below this many characters a page is treated as having no usable text layer.
# A scanned page is not always empty — a bank's cover sheet may carry a few
# stamped words — so "sparse" rather than "absent" is the test.
MIN_CHARS_PER_TEXT_PAGE = 40

# A page whose text is this sparse relative to its area is almost certainly an
# image with a caption rather than a transaction table.
MIN_CHARS_PER_SQUARE_INCH = 0.5


@dataclass(frozen=True, slots=True)
class Word:
    """One run of text and where it sits on the page.

    Coordinates are PDF user space: origin bottom-left, y increasing upward.
    Kept in that space rather than normalised so that a run's own height can be
    used as the tolerance for "same line", which is what makes line grouping
    work across statements set in different point sizes.
    """

    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    page: int

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return self.y1 - self.y0


class NoTextLayerError(ValidationError):
    """The PDF is scanned, or its text layer is unusable.

    A distinct type because the response is specific and actionable: v1 does
    not OCR statements, and the honest answer is to say so and point at the
    bank's CSV export rather than silently producing a worse import.
    """


class EncryptedPdfError(ValidationError):
    """Password-protected. Refused rather than prompted for.

    Asking for a statement password would mean holding one, which is a
    liability this product should not take on for a convenience.
    """


def _page_words(textpage: pdfium.PdfTextPage, page_index: int) -> list[Word]:
    """Every text run on one page, with its box.

    pypdfium2 exposes runs as rectangles; each is a contiguous piece of text
    laid out together, which is exactly the granularity wanted here — a whole
    description cell arrives as one run rather than as loose characters to be
    reassembled.
    """
    words: list[Word] = []
    for index in range(textpage.count_rects()):
        left, bottom, right, top = textpage.get_rect(index)
        text = textpage.get_text_bounded(
            left=left, bottom=bottom, right=right, top=top
        ).strip()
        if not text:
            continue
        words.append(
            Word(text=text, x0=left, y0=bottom, x1=right, y1=top, page=page_index)
        )
    return words


def extract_pages(data: bytes) -> list[list[Word]]:
    """Words per page, or a refusal that says which problem it is.

    Raises EncryptedPdfError, NoTextLayerError or ValidationError — never a
    partial result. A statement that cannot be read completely is not imported
    partially: half a month of transactions looks exactly like a full month to
    somebody reviewing totals.
    """
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        message = str(exc).lower()
        if "password" in message or "encrypt" in message:
            raise EncryptedPdfError(
                "That PDF is password-protected. Please save an unprotected copy "
                "and upload that instead."
            ) from exc
        raise ValidationError("That PDF could not be opened.") from exc

    try:
        page_count = len(document)
        if page_count == 0:
            raise ValidationError("That PDF has no pages.")
        if page_count > settings.max_statement_pages:
            raise ValidationError(
                f"That statement has {page_count} pages; the limit is "
                f"{settings.max_statement_pages}."
            )

        pages: list[list[Word]] = []
        total_chars = 0
        for index in range(page_count):
            page = document[index]
            textpage = page.get_textpage()
            try:
                total_chars += textpage.count_chars()
                pages.append(_page_words(textpage, index))
            finally:
                textpage.close()

        if total_chars < MIN_CHARS_PER_TEXT_PAGE * max(1, page_count // 4):
            raise NoTextLayerError(
                "That statement looks like a scan rather than a PDF with real "
                "text in it. Ledger AI reads text-layer statements only. Most "
                "banks offer a CSV export — that will import cleanly."
            )

        logger.info(
            "statement.extracted pages=%d runs=%d",
            page_count,
            sum(len(p) for p in pages),
        )
        return pages
    finally:
        document.close()
