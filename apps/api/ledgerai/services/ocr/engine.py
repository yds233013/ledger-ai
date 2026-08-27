"""Text extraction.

`OcrEngine` is a Protocol so every parser test can run against a fake engine
with a hand-written word list — no Tesseract binary, no image rendering, and
completely deterministic assertions.

Per-word confidences come from Tesseract's TSV output. They are what makes
field-level confidence possible: a field's score is the mean confidence of the
words that actually produced it, not a guess about the document as a whole.
"""

from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Protocol

import pytesseract
from PIL import Image

logger = logging.getLogger(__name__)

# Tesseract emits -1 for non-text layout rows; those are not words.
_NO_CONFIDENCE = -1.0

# 6 = "assume a single uniform block of text", which is what a receipt is.
# 4 = "single column of text of variable sizes" handles wider layouts.
PSM_CANDIDATES = (6, 4)


@dataclass(slots=True, frozen=True)
class OcrWord:
    text: str
    confidence: float  # 0.0–1.0
    line: int


@dataclass(slots=True)
class OcrResult:
    text: str = ""
    words: list[OcrWord] = field(default_factory=list)
    engine: str = "tesseract"
    page_count: int = 1

    @property
    def mean_confidence(self) -> float:
        """Document-level confidence: the mean over real words."""
        if not self.words:
            return 0.0
        return sum(word.confidence for word in self.words) / len(self.words)

    def lines(self) -> list[list[OcrWord]]:
        grouped: dict[int, list[OcrWord]] = {}
        for word in self.words:
            grouped.setdefault(word.line, []).append(word)
        return [grouped[key] for key in sorted(grouped)]


class OcrEngine(Protocol):
    name: str

    def extract(self, images: list[Image.Image]) -> OcrResult: ...


class TesseractEngine:
    """Local Tesseract. No network, no third party, no data leaves the machine."""

    name = "tesseract"

    def __init__(self, language: str = "eng") -> None:
        self._language = language

    def extract(self, images: list[Image.Image]) -> OcrResult:
        """Try each page-segmentation mode and keep the most confident read.

        Receipts vary a lot in layout; a mode that suits a narrow thermal
        receipt does poorly on a wide printed invoice. Trying both and scoring
        is cheap and deterministic.
        """
        best: OcrResult | None = None

        for psm in PSM_CANDIDATES:
            words: list[OcrWord] = []
            texts: list[str] = []
            line_offset = 0

            for image in images:
                config = f"--psm {psm}"
                try:
                    tsv = pytesseract.image_to_data(
                        image, lang=self._language, config=config
                    )
                    texts.append(
                        pytesseract.image_to_string(image, lang=self._language, config=config)
                    )
                except pytesseract.TesseractError:
                    # A mode that cannot handle this page is not an error; the
                    # other mode may succeed. Logged without any page content.
                    logger.warning("OCR page failed at psm=%s", psm)
                    continue

                page_words, max_line = _parse_tsv(tsv, line_offset)
                words.extend(page_words)
                line_offset = max_line + 1

            candidate = OcrResult(
                text="\n".join(texts).strip(),
                words=words,
                engine=self.name,
                page_count=len(images),
            )
            # Prefer the read that found more words, breaking ties on confidence.
            if best is None or (len(candidate.words), candidate.mean_confidence) > (
                len(best.words),
                best.mean_confidence,
            ):
                best = candidate

        return best or OcrResult(engine=self.name, page_count=len(images))


def _parse_tsv(tsv: str, line_offset: int) -> tuple[list[OcrWord], int]:
    words: list[OcrWord] = []
    max_line = line_offset

    for row in csv.DictReader(io.StringIO(tsv), delimiter="\t"):
        text = (row.get("text") or "").strip()
        if not text:
            continue
        try:
            confidence = float(row.get("conf", _NO_CONFIDENCE))
            block = int(row.get("block_num", 0))
            par = int(row.get("par_num", 0))
            line = int(row.get("line_num", 0))
        except (TypeError, ValueError):
            continue
        if confidence < 0:
            continue

        # Tesseract restarts line numbering per block/paragraph, so compose a
        # key that stays ordered across the whole page.
        composed = line_offset + block * 10_000 + par * 100 + line
        max_line = max(max_line, composed)
        words.append(OcrWord(text=text, confidence=confidence / 100.0, line=composed))

    return words, max_line


def build_engine() -> OcrEngine:
    return TesseractEngine()
