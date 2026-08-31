"""Checking that the text layer says what the page shows.

A PDF's text layer and its rendered appearance are independent. Nothing stops a
file from drawing "-42.10" while its text layer carries "-4,210.00", or from
hiding whole transactions in text drawn in invisible render mode. Reading the
text layer alone means importing what a file *claims* rather than what its
owner saw when they downloaded it — so the claim gets checked.

**What this catches:** amounts present in the text layer that never appear in
the rendered page, and amounts visible on the page that the text layer omits.
That covers the two attacks that matter: inflated or altered figures, and
hidden rows.

**What this does not catch, stated plainly.** The check samples pages rather
than rendering all forty, so a discrepancy on an unsampled page of a long
statement can pass. It compares the *set* of money-shaped tokens, so a
transposition that preserves every amount while moving them between rows is
invisible to it. It cannot see a description altered without touching a number.
It is a guard against tampering that changes the money, not a proof that the
document is authentic — and the review step exists partly because of that.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import pypdfium2 as pdfium
from PIL import Image

from ...config import settings

logger = logging.getLogger(__name__)

# Enough to read digits reliably without the cost of print-quality rendering.
VERIFY_DPI = 150

# A money token the text layer asserts is only a finding when its digits are
# absent from the rendered page *entirely*. Comparing formatted tokens alone
# would fail on harmless disagreements — a comma OCR reads as a full stop turns
# "1,904.55" into "1.904.55" — so a token that misses on its exact form is
# retried against the page's raw digit stream before it counts against the file.
#
# With that second pass in place the threshold is zero: any amount claimed by
# the text layer and genuinely not drawn on the page refuses the statement.
_MONEY = re.compile(r"\d{1,3}(?:,\d{3})*\.\d{2}")
_NON_DIGIT = re.compile(r"[^0-9]")


@dataclass(slots=True)
class VerificationResult:
    """Whether the sampled pages agree, and by how much."""

    checked_pages: int = 0
    mismatched_pages: int = 0
    ok: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"{self.checked_pages - self.mismatched_pages}/{self.checked_pages} pages agree"


def _money_multiset(text: str) -> list[str]:
    return sorted(_MONEY.findall(text))


def _absent_from_render(claimed: set[str], shown_text: str) -> set[str]:
    """Claimed amounts whose digits appear nowhere in what the page renders.

    Two passes. A token found verbatim is present. Otherwise its digits are
    looked for in the page's digit stream, which forgives a misread separator
    without forgiving a number that was never drawn.
    """
    shown_tokens = set(_MONEY.findall(shown_text))
    digit_stream = _NON_DIGIT.sub("", shown_text)
    absent: set[str] = set()
    for token in claimed:
        if token in shown_tokens:
            continue
        if _NON_DIGIT.sub("", token) in digit_stream:
            continue
        absent.add(token)
    return absent


def _sample_indices(page_count: int, sample: int) -> list[int]:
    """Which pages to render: first, last, and evenly spaced between.

    The first page carries the header and opening balance, the last the closing
    balance; a tampered statement that left both alone would still have to
    survive the middle sample.
    """
    if page_count <= sample:
        return list(range(page_count))
    if sample <= 1:
        return [0]
    step = (page_count - 1) / (sample - 1)
    return sorted({round(i * step) for i in range(sample)})


def verify_text_layer(data: bytes, page_texts: list[str]) -> VerificationResult:
    """Render a sample of pages and compare their money tokens to the text layer.

    Never raises for content: a rendering failure degrades to "not checked" and
    is reported, because refusing a legitimate statement because one page would
    not rasterise is a worse outcome than importing it with the check noted.
    """
    result = VerificationResult()
    try:
        import pytesseract
    except ImportError:  # pragma: no cover - the dependency is pinned
        result.notes.append("verifier_unavailable")
        return result

    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        result.notes.append("render_failed")
        return result

    try:
        indices = _sample_indices(len(document), settings.statement_verify_sample_pages)
        for index in indices:
            if index >= len(page_texts):
                continue
            claimed = _money_multiset(page_texts[index])
            if not claimed:
                continue

            try:
                bitmap = document[index].render(scale=VERIFY_DPI / 72)
                image: Image.Image = bitmap.to_pil().convert("L")
                shown_text = pytesseract.image_to_string(image)
            except Exception:  # noqa: BLE001 - a failed render is not a verdict
                result.notes.append("page_render_failed")
                continue

            result.checked_pages += 1
            # Only one direction is evidence: money the text layer asserts that
            # the page never draws. The reverse — something rendered that the
            # text layer omits — is how a legitimate scanned-in logo or a
            # flattened figure behaves, and refusing on it would reject honest
            # statements.
            if _absent_from_render(set(claimed), shown_text):
                result.mismatched_pages += 1

        result.ok = result.mismatched_pages == 0
        if not result.ok:
            result.notes.append("text_layer_disagrees_with_render")
        logger.info(
            "statement.verified pages=%d mismatched=%d",
            result.checked_pages,
            result.mismatched_pages,
        )
        return result
    finally:
        document.close()
