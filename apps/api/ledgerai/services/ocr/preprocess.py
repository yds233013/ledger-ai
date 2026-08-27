"""Turn an uploaded file into page images Tesseract can read.

Everything here is deterministic so OCR fixtures stay stable across runs.

Security notes:
  * Pillow's decompression-bomb ceiling is set explicitly and the dimensions
    are checked before a full decode.
  * Images are re-encoded, which strips EXIF — receipt photos routinely carry
    GPS coordinates, and none of that should reach storage or the logs.
"""

from __future__ import annotations

import io
import logging

import pypdfium2 as pdfium
from PIL import Image, ImageOps
from PIL.Image import Resampling

from ...security.validators import ValidationError

logger = logging.getLogger(__name__)

MAX_PIXELS = 40_000_000          # 40 MP
MAX_DIMENSION = 10_000           # px on any side
MAX_PDF_PAGES = 5
PDF_RENDER_DPI = 200
# Below this, upscaling meaningfully improves Tesseract's accuracy.
MIN_OCR_WIDTH = 1000
MAX_OCR_WIDTH = 2600

# Pillow's own guard. Set explicitly rather than relying on the default.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def load_pages(data: bytes, content_type: str) -> list[Image.Image]:
    """Rasterize an upload into page images, enforcing the safety limits."""
    if content_type == "application/pdf":
        return _load_pdf(data)
    return [_load_image(data)]


def _load_image(data: bytes) -> Image.Image:
    try:
        probe = Image.open(io.BytesIO(data))
        width, height = probe.size
    except Exception as exc:  # noqa: BLE001 - any decode failure is user-facing
        raise ValidationError("That image could not be read.") from exc

    if width > MAX_DIMENSION or height > MAX_DIMENSION:
        raise ValidationError(
            f"Image is {width}×{height}px; the limit is {MAX_DIMENSION}px on a side."
        )
    if width * height > MAX_PIXELS:
        raise ValidationError(
            f"Image is {width * height / 1_000_000:.0f} megapixels; the limit is "
            f"{MAX_PIXELS // 1_000_000}."
        )

    try:
        opened: Image.Image = Image.open(io.BytesIO(data))
        # Honour the EXIF orientation, then drop EXIF entirely by converting.
        rotated = ImageOps.exif_transpose(opened) or opened
        return rotated.convert("L")
    except Image.DecompressionBombError as exc:
        raise ValidationError("That image is too large to process safely.") from exc
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("That image could not be decoded.") from exc


def _load_pdf(data: bytes) -> list[Image.Image]:
    try:
        document = pdfium.PdfDocument(data)
    except Exception as exc:  # noqa: BLE001
        raise ValidationError("That PDF could not be opened.") from exc

    page_count = len(document)
    if page_count == 0:
        raise ValidationError("That PDF has no pages.")
    if page_count > MAX_PDF_PAGES:
        raise ValidationError(
            f"PDF has {page_count} pages; the limit is {MAX_PDF_PAGES} for a receipt."
        )

    pages: list[Image.Image] = []
    scale = PDF_RENDER_DPI / 72
    for index in range(page_count):
        try:
            bitmap = document[index].render(scale=scale)
            pages.append(bitmap.to_pil().convert("L"))
        except Exception as exc:  # noqa: BLE001
            raise ValidationError(f"Page {index + 1} of that PDF could not be rendered.") from exc
    return pages


def prepare_for_ocr(image: Image.Image) -> Image.Image:
    """Grayscale, normalize contrast, and scale into Tesseract's happy range."""
    prepared = image.convert("L")
    prepared = ImageOps.autocontrast(prepared, cutoff=1)

    width, height = prepared.size
    if width < MIN_OCR_WIDTH and width > 0:
        factor = MIN_OCR_WIDTH / width
        prepared = prepared.resize(
            (int(width * factor), int(height * factor)), Resampling.LANCZOS
        )
    elif width > MAX_OCR_WIDTH:
        factor = MAX_OCR_WIDTH / width
        prepared = prepared.resize(
            (int(width * factor), int(height * factor)), Resampling.LANCZOS
        )
    return prepared


def render_preview(image: Image.Image, max_width: int = 1400) -> bytes:
    """Server-side PNG preview.

    A PDF receipt is rasterized here rather than handed to the browser, so the
    review page never asks a viewer to execute an untrusted PDF.
    """
    preview = image.convert("L")
    if preview.width > max_width:
        factor = max_width / preview.width
        preview = preview.resize(
            (max_width, int(preview.height * factor)), Resampling.LANCZOS
        )
    buffer = io.BytesIO()
    preview.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
