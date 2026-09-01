"""Checking that the text layer says what the page shows.

A PDF's text layer and its rendered appearance are independent. Nothing stops a
file from drawing "-42.10" while its text layer carries "-4,210.00", or from
hiding whole transactions in text drawn in invisible render mode. Reading the
text layer alone means importing what a file *claims* rather than what its
owner saw when they downloaded it — so the claim gets checked.

**How the claim is checked.** Every money token in the text layer is located by
its own character boxes, so each one is compared against the pixels it occupies
rather than against a bag of values collected from the whole document. A bag
comparison cannot see a value that moved between rows, and it fails a dense page
whenever OCR misreads any one of sixty amounts. Position fixes both.

**Why a token that OCR did not read is not automatically a finding.** OCR is
probabilistic; the page either drew the glyphs or it did not. So an unread token
is measured for ink — actual dark pixels inside that token's own box — and only
a token with ink behind it may draw on the bounded omission budget, after a
second, focused read at higher resolution has also failed. A token with no ink
is a conflict, whatever its neighbours look like. That distinction is the whole
point: white-on-white text, clipped text and invisible render mode all leave the
row around them perfectly legible, so a row-level liveness test would wave them
through. The four cases the classifier separates are a visible value OCR missed,
an invisible or white or clipped value, different digits rendered, and nothing
drawn at all — and only the first is forgivable.

**What this does not catch, stated plainly.** A statement whose text layer and
rendering agree with each other but disagree with the bank is out of scope; this
compares a file against itself. A transposition that swaps two amounts in the
text layer *and* the rendering leaves nothing to see here, and is left to the
balance chain in `parse` where balances exist. It cannot see a description
altered without touching a number. It is a guard against tampering that changes
the money, not a proof that the document is authentic — and the review step
exists partly because of that.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

import pypdfium2 as pdfium
from PIL import Image

from ...config import settings

logger = logging.getLogger(__name__)

_MONEY = re.compile(r"\d{1,3}(?:[,. ]\d{3})*[.,]\d{2}")
_NON_DIGIT = re.compile(r"[^0-9]")

# Grey level below which a pixel counts as glyph ink. Rendered 9pt digits fill
# 15-32% of their own character box; hidden, white and clipped text fill
# exactly none of it. The threshold sits far below the first and far above the
# second, so it separates the two without needing to be tuned.
_INK_LEVEL = 200
_INK_MIN_FRACTION = 0.004

# The token box is the glyphs themselves. A wide pad would pull in a neighbour's
# ink and hand an attacker a way to make a blank region look inked.
_PAD_POINTS = 1.0


@dataclass(slots=True)
class TokenCounts:
    """How one page's claimed money tokens compared against its rendering."""

    total: int = 0
    matched: int = 0
    formatting: int = 0
    omitted: int = 0
    conflicts: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    @property
    def coverage(self) -> float:
        if not self.total:
            return 1.0
        return (self.matched + self.formatting + self.omitted) / self.total

    def _flag(self, reason: str) -> None:
        self.conflicts += 1
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


@dataclass(slots=True)
class VerificationResult:
    """Whether every checked page agrees, and by how much."""

    checked_pages: int = 0
    mismatched_pages: int = 0
    retried_pages: int = 0
    ok: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        return f"{self.checked_pages - self.mismatched_pages}/{self.checked_pages} pages agree"


def _digits(value: str) -> str:
    return _NON_DIGIT.sub("", value)


def _claimed_tokens(
    document: pdfium.PdfDocument, index: int
) -> list[tuple[str, float, float, float, float]]:
    """Money tokens on one page, each with the box its own characters occupy.

    Character boxes rather than the coarser rect boxes: a rect can span a whole
    line, and a box that wide would report a neighbouring figure's ink as this
    token's own.
    """
    textpage = document[index].get_textpage()
    tokens: list[tuple[str, float, float, float, float]] = []
    try:
        text = textpage.get_text_range()
        char_count = textpage.count_chars()
        for match in _MONEY.finditer(text):
            box: tuple[float, float, float, float] | None = None
            for position in range(match.start(), min(match.end(), char_count)):
                try:
                    left, bottom, right, top = textpage.get_charbox(position)
                except Exception:  # noqa: BLE001, S112 - one unreadable glyph is not a verdict
                    continue
                if right <= left or top <= bottom:
                    continue
                box = (
                    (left, bottom, right, top)
                    if box is None
                    else (
                        min(box[0], left),
                        min(box[1], bottom),
                        max(box[2], right),
                        max(box[3], top),
                    )
                )
            if box is not None:
                tokens.append((match.group(), *box))
    finally:
        textpage.close()
    return tokens


def _ocr_words(  # noqa: ANN001
    image: Image.Image, pytesseract
) -> list[tuple[str, int, int, int, int]]:
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    words: list[tuple[str, int, int, int, int]] = []
    for position in range(len(data["text"])):
        text = (data["text"][position] or "").strip()
        if not text:
            continue
        left, top = data["left"][position], data["top"][position]
        words.append(
            (text, left, top, left + data["width"][position], top + data["height"][position])
        )
    return words


def _ink_fraction(image: Image.Image, box: tuple[float, float, float, float]) -> float:
    """Share of pixels inside the box dark enough to be glyph ink."""
    left, top = max(0, int(box[0])), max(0, int(box[1]))
    right, bottom = min(image.width, int(box[2]) + 1), min(image.height, int(box[3]) + 1)
    if right <= left or bottom <= top:
        return 0.0
    histogram = image.crop((left, top, right, bottom)).histogram()
    total = sum(histogram)
    if not total:
        return 0.0
    return sum(histogram[:_INK_LEVEL]) / total


def _focused_read(
    cache: dict[str, Image.Image | None],
    document: pdfium.PdfDocument,
    index: int,
    box: tuple[float, float, float, float],
    page_height: float,
    pytesseract,  # noqa: ANN001
) -> str:
    """Re-read one token's region alone, at high resolution, in single-line mode.

    Built at most once per page and reused: a page with several unread tokens
    must not pay for the render several times over.
    """
    scale = settings.statement_verify_focus_dpi / 72
    if cache.get("image") is None:
        try:
            cache["image"] = document[index].render(scale=scale).to_pil().convert("L")
        except Exception:  # noqa: BLE001 - fall back to "could not read"
            return ""
    image = cache["image"]
    if image is None:
        return ""
    left, bottom, right, top = box
    pad = 3
    crop = image.crop(
        (
            max(0, int(left * scale) - pad),
            max(0, int((page_height - top) * scale) - pad),
            min(image.width, int(right * scale) + pad),
            min(image.height, int((page_height - bottom) * scale) + pad),
        )
    )
    if crop.width < 4 or crop.height < 4:
        return ""
    try:
        return pytesseract.image_to_string(crop, config="--psm 7").strip()
    except Exception:  # noqa: BLE001 - a failed read is "could not read", not a verdict
        return ""


def _analyse_page(
    document: pdfium.PdfDocument, index: int, dpi: int, pytesseract  # noqa: ANN001
) -> TokenCounts | None:
    """Compare one page's claimed money tokens against what it draws."""
    tokens = _claimed_tokens(document, index)
    if not tokens:
        return None

    page = document[index]
    _, page_height = page.get_size()
    scale = dpi / 72
    image: Image.Image = page.render(scale=scale).to_pil().convert("L")
    words = _ocr_words(image, pytesseract)

    counts = TokenCounts(total=len(tokens))
    focus_cache: dict[str, Image.Image | None] = {"image": None}
    pad = _PAD_POINTS * scale

    for token, left, bottom, right, top in tokens:
        region = (
            left * scale - pad,
            (page_height - top) * scale - pad,
            right * scale + pad,
            (page_height - bottom) * scale + pad,
        )
        overlapping = [
            word
            for word in words
            if not (
                word[3] < region[0]
                or word[1] > region[2]
                or word[4] < region[1]
                or word[2] > region[3]
            )
        ]
        rendered = "".join(word[0] for word in overlapping)
        claimed_digits = _digits(token)

        if _digits(rendered):
            if token in rendered:
                counts.matched += 1
            elif claimed_digits and claimed_digits in _digits(rendered):
                # A comma OCR read as a full stop is a rendering artefact, not a
                # different number.
                counts.formatting += 1
            else:
                counts._flag("different_digits")
            continue

        # Nothing was read here. The pixels decide, not the neighbouring row.
        if _ink_fraction(image, region) < _INK_MIN_FRACTION:
            counts._flag("no_ink")
            continue

        # There is ink, so the value really is drawn. Try harder before spending
        # the omission budget on it.
        read = _focused_read(
            focus_cache, document, index, (left, bottom, right, top), page_height, pytesseract
        )
        read_digits = _digits(read)
        if not read_digits:
            counts.omitted += 1
        elif token in read or (claimed_digits and claimed_digits in read_digits):
            counts.matched += 1
        else:
            counts._flag("different_digits")

    return counts


def _within_budget(counts: TokenCounts) -> bool:
    budget = max(
        settings.statement_verify_max_omissions,
        int(counts.total * settings.statement_verify_omission_fraction),
    )
    return (
        counts.conflicts == 0
        and counts.omitted <= budget
        and counts.coverage >= settings.statement_verify_min_coverage
    )


def verify_text_layer(data: bytes, page_texts: list[str] | None = None) -> VerificationResult:
    """Compare every page's money tokens against what that page renders.

    `page_texts` is accepted and ignored: the check needs character positions,
    which the extracted word list has already discarded, so it reads the text
    layer itself. The parameter stays so the caller does not have to change.

    Never raises for content. A rendering failure degrades to "not checked" and
    is reported, because refusing a legitimate statement because one page would
    not rasterise is a worse outcome than importing it with the check noted. A
    page that *is* checked and remains inconclusive after the higher-resolution
    retry fails closed.
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
        for index in range(len(document)):
            try:
                counts = _analyse_page(
                    document, index, settings.statement_verify_dpi, pytesseract
                )
            except Exception:  # noqa: BLE001 - a failed render is not a verdict
                result.notes.append("page_render_failed")
                continue

            # A page with no money on it carries no claim to check.
            if counts is None:
                continue

            result.checked_pages += 1
            if _within_budget(counts):
                continue

            # Inconclusive at the base resolution. Re-read this page alone at a
            # higher one before deciding — most first-pass failures are OCR
            # struggling with small type, not tampering.
            result.retried_pages += 1
            try:
                counts = _analyse_page(
                    document, index, settings.statement_verify_retry_dpi, pytesseract
                )
            except Exception:  # noqa: BLE001
                result.notes.append("page_render_failed")

            if counts is None or not _within_budget(counts):
                result.mismatched_pages += 1
                if counts is not None:
                    for reason in sorted(counts.reasons):
                        note = f"page_{reason}"
                        if note not in result.notes:
                            result.notes.append(note)
                    if not counts.reasons:
                        note = "page_coverage_insufficient"
                        if note not in result.notes:
                            result.notes.append(note)

        result.ok = result.mismatched_pages == 0
        if not result.ok:
            result.notes.append("text_layer_disagrees_with_render")
        logger.info(
            "statement.verified pages=%d mismatched=%d retried=%d",
            result.checked_pages,
            result.mismatched_pages,
            result.retried_pages,
        )
        return result
    finally:
        document.close()
