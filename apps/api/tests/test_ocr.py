"""OCR parsing against synthetic fixtures.

Every test here drives a **fake engine** with a hand-written word list, so the
assertions are exact and the suite needs no Tesseract binary, no image
rendering and no real receipt. The hazards guarded against were all found by
running real Tesseract during development.
"""

from __future__ import annotations

import io
from datetime import date

import pytest
from PIL import Image

from ledgerai.security.validators import ValidationError, detect_kind
from ledgerai.services.ocr.engine import OcrResult, OcrWord
from ledgerai.services.ocr.parse import (
    FIELD_REVIEW_THRESHOLD,
    last_money_on_line,
    money_to_cents,
    parse_receipt,
)
from ledgerai.services.ocr.preprocess import (
    MAX_PDF_PAGES,
    load_pages,
    prepare_for_ocr,
    render_preview,
)


def make_result(lines: list[str], confidence: float = 0.95) -> OcrResult:
    """Build an OcrResult from plain text lines, as if OCR had produced them."""
    words: list[OcrWord] = []
    for index, line in enumerate(lines):
        for token in line.split():
            words.append(OcrWord(text=token, confidence=confidence, line=index))
    return OcrResult(text="\n".join(lines), words=words, engine="fake")


CLEAN_RECEIPT = [
    "SANDBOX GROCERS",
    "*** SYNTHETIC DEMO ***",
    "1 Example Way, Sandbox",
    "Date: 08/14/2026 14:32",
    "----------------------------",
    "Oat Milk 1L 4.99",
    "Sourdough Loaf 6.50",
    "----------------------------",
    "SUBTOTAL 28.05",
    "TAX 8.25% 2.31",
    "TIP 0.00",
    "TOTAL 30.36",
    "NOT A REAL RECEIPT",
]


class TestMoneyParsing:
    @pytest.mark.parametrize(
        ("whole", "fraction", "cents"),
        [("30", "36", 3036), ("1,234", "56", 123456), ("0", "99", 99)],
    )
    def test_money_to_cents(self, whole: str, fraction: str, cents: int) -> None:
        assert money_to_cents(whole, fraction) == cents

    def test_comma_decimal_separator_is_accepted(self) -> None:
        """Tesseract emits 4,99 for 4.99 — a real observed misread."""
        assert last_money_on_line("TOTAL 4,99") == (499, "4,99")

    def test_percentage_is_not_mistaken_for_an_amount(self) -> None:
        """`TAX 8.25%  2.31` must yield 2.31, not 8.25."""
        found = last_money_on_line("TAX 8.25% 2.31")
        assert found is not None
        assert found[0] == 231

    def test_quantities_without_decimals_are_ignored(self) -> None:
        assert last_money_on_line("Order 88213 Lane 04") is None


class TestFieldExtraction:
    def test_extracts_every_field_from_a_clean_receipt(self) -> None:
        parsed = parse_receipt(make_result(CLEAN_RECEIPT))
        assert parsed.merchant == "SANDBOX GROCERS"
        assert parsed.posted_date == date(2026, 8, 14)
        assert parsed.subtotal_cents == 2805
        assert parsed.tax_cents == 231
        assert parsed.tip_cents == 0
        assert parsed.total_cents == 3036
        assert parsed.currency == "USD"

    def test_subtotal_is_never_read_as_total(self) -> None:
        """The hazard that motivated line-anchored matching: a naive
        `TOTAL\\s+([\\d.]+)` matches inside SUBTOTAL and returns 28.05."""
        parsed = parse_receipt(make_result(["SUBTOTAL 28.05", "TOTAL 30.36"]))
        assert parsed.subtotal_cents == 2805
        assert parsed.total_cents == 3036

    def test_grand_total_wins_over_total(self) -> None:
        parsed = parse_receipt(make_result(["SUBTOTAL 10.00", "GRAND TOTAL 12.50"]))
        assert parsed.total_cents == 1250

    def test_extracted_amounts_are_positive(self) -> None:
        """The receipt keeps what was printed; the outflow sign is applied when
        the transaction is created, not here."""
        parsed = parse_receipt(make_result(CLEAN_RECEIPT))
        assert parsed.total_cents is not None
        assert parsed.total_cents > 0
        assert parsed.subtotal_cents is not None
        assert parsed.subtotal_cents > 0

    @pytest.mark.parametrize(
        ("line", "expected"),
        [("TOTAL $30.36", "USD"), ("TOTAL €30.36", "EUR"), ("TOTAL £30.36", "GBP")],
    )
    def test_currency_detected_from_symbol(self, line: str, expected: str) -> None:
        assert parse_receipt(make_result(["SANDBOX SHOP", line])).currency == expected

    def test_currency_defaults_to_usd_with_a_note(self) -> None:
        parsed = parse_receipt(make_result(["SANDBOX SHOP", "TOTAL 30.36"]))
        assert parsed.currency == "USD"
        assert "assumed USD" in parsed.notes["currency"]

    def test_merchant_skips_address_and_noise_lines(self) -> None:
        parsed = parse_receipt(
            make_result(
                [
                    "*** SYNTHETIC DEMO ***",
                    "123 Example Street",
                    "SANDBOX HARDWARE CO",
                    "TOTAL 12.00",
                ]
            )
        )
        assert parsed.merchant == "SANDBOX HARDWARE CO"

    def test_future_dated_receipt_is_rejected_as_a_misread(self) -> None:
        parsed = parse_receipt(make_result(["SANDBOX SHOP", "Date: 01/01/2099"]))
        assert parsed.posted_date is None


class TestConsistencyAndConfidence:
    def test_matching_arithmetic_raises_confidence(self) -> None:
        parsed = parse_receipt(make_result(CLEAN_RECEIPT, confidence=0.80))
        assert parsed.is_arithmetically_consistent is True
        assert parsed.field_confidence["total"] > 0.80

    def test_mismatched_arithmetic_lowers_confidence_and_forces_review(self) -> None:
        """A misread digit shows up as parts that do not add to the total."""
        parsed = parse_receipt(
            make_result(["SANDBOX SHOP", "Date: 08/14/2026", "SUBTOTAL 28.05",
                         "TAX 2.31", "TOTAL 99.99"])
        )
        assert parsed.is_arithmetically_consistent is False
        assert parsed.needs_review is True
        assert "but the total" in parsed.notes["consistency"]

    def test_low_confidence_words_force_review(self) -> None:
        parsed = parse_receipt(make_result(CLEAN_RECEIPT, confidence=0.40))
        assert parsed.needs_review is True

    def test_clean_high_confidence_receipt_does_not_need_review(self) -> None:
        parsed = parse_receipt(make_result(CLEAN_RECEIPT, confidence=0.97))
        assert parsed.needs_review is False

    def test_missing_total_always_needs_review(self) -> None:
        parsed = parse_receipt(make_result(["SANDBOX SHOP", "Date: 08/14/2026"]))
        assert parsed.total_cents is None
        assert parsed.needs_review is True

    def test_assumed_currency_alone_does_not_force_review(self) -> None:
        """Currency is reported with its confidence, but an assumed USD is a
        disclosure, not a reason to re-check an otherwise clean receipt."""
        parsed = parse_receipt(make_result(CLEAN_RECEIPT, confidence=0.97))
        assert parsed.field_confidence["currency"] < FIELD_REVIEW_THRESHOLD
        assert parsed.needs_review is False

    def test_unlabelled_total_falls_back_and_says_so(self) -> None:
        parsed = parse_receipt(
            make_result(["SANDBOX SHOP", "Date: 08/14/2026", "Item A 4.00", "Item B 19.50"])
        )
        assert parsed.total_cents == 1950
        assert "No line was labelled TOTAL" in parsed.notes["total"]
        assert parsed.needs_review is True

    def test_empty_document_is_handled(self) -> None:
        parsed = parse_receipt(OcrResult())
        assert parsed.total_cents is None
        assert parsed.needs_review is True
        assert "No text" in parsed.notes["ocr"]


class TestPreprocessing:
    @staticmethod
    def _image_bytes(fmt: str, size: tuple[int, int] = (400, 300)) -> bytes:
        buffer = io.BytesIO()
        Image.new("RGB", size, "white").save(buffer, format=fmt)
        return buffer.getvalue()

    def test_png_loads_as_a_single_grayscale_page(self) -> None:
        pages = load_pages(self._image_bytes("PNG"), "image/png")
        assert len(pages) == 1
        assert pages[0].mode == "L"

    def test_pdf_rasterizes_without_a_system_dependency(self) -> None:
        pages = load_pages(self._image_bytes("PDF"), "application/pdf")
        assert len(pages) == 1
        assert pages[0].mode == "L"

    def test_oversized_image_is_rejected_before_decoding(self) -> None:
        oversized = Image.new("L", (12_000, 10), "white")
        buffer = io.BytesIO()
        oversized.save(buffer, format="PNG")
        with pytest.raises(ValidationError, match="limit is"):
            load_pages(buffer.getvalue(), "image/png")

    def test_corrupt_image_gives_a_user_facing_error(self) -> None:
        with pytest.raises(ValidationError):
            load_pages(b"not an image at all", "image/png")

    def test_preview_renders_png_bytes(self) -> None:
        page = load_pages(self._image_bytes("PNG"), "image/png")[0]
        preview = render_preview(page)
        assert preview.startswith(b"\x89PNG")

    def test_small_images_are_upscaled_for_ocr(self) -> None:
        page = load_pages(self._image_bytes("PNG", (300, 200)), "image/png")[0]
        assert prepare_for_ocr(page).width > page.width

    def test_pdf_page_cap_is_documented(self) -> None:
        assert MAX_PDF_PAGES == 5


class TestUploadTypeDetection:
    def test_pdf_is_accepted_as_a_receipt(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (50, 50), "white").save(buffer, format="PDF")
        kind, mime = detect_kind("receipt.pdf", buffer.getvalue())
        assert mime == "application/pdf"

    def test_pdf_extension_with_non_pdf_content_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not a valid PDF"):
            detect_kind("receipt.pdf", b"definitely not a pdf")
