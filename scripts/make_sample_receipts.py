#!/usr/bin/env python
"""Generate the synthetic sample receipts shipped in docs/samples/receipts/.

Every receipt is fabricated. Each one is stamped *** SYNTHETIC DEMO *** and
NOT A REAL RECEIPT, uses sandbox merchant names, and carries no real address,
card number or person. No real receipt is used at any point.

A deliberately degraded variant exercises the low-confidence review path.
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "docs" / "samples" / "receipts"
SEED = 90210

FONT_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Courier New.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
]
BOLD_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/Courier New Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
]


def _font(paths: list[str], size: int) -> ImageFont.ImageFont:
    for path in paths:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render(lines: list[tuple[bool, str]], width: int = 470) -> Image.Image:
    regular = _font(FONT_CANDIDATES, 17)
    bold = _font(BOLD_CANDIDATES, 20)
    height = 46 + len(lines) * 30

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    y = 24
    for is_bold, text in lines:
        draw.text((24, y), text, font=(bold if is_bold else regular), fill=0)
        y += 30
    return image


def receipt_lines(
    merchant: str,
    date_text: str,
    items: list[tuple[str, str]],
    subtotal: str,
    tax: str,
    tip: str,
    total: str,
    currency_symbol: str = "",
) -> list[tuple[bool, str]]:
    lines: list[tuple[bool, str]] = [
        (True, merchant),
        (False, "  *** SYNTHETIC DEMO ***"),
        (False, "  1 Example Way, Sandbox"),
        (False, ""),
        (False, f"Date: {date_text}"),
        (False, "Order 88213   Lane 04"),
        (False, "-" * 34),
    ]
    lines += [(False, f"{name:<20}{amount:>10}") for name, amount in items]
    lines += [
        (False, "-" * 34),
        (False, f"{'SUBTOTAL':<20}{currency_symbol + subtotal:>10}"),
        (False, f"{'TAX 8.25%':<20}{currency_symbol + tax:>10}"),
        (False, f"{'TIP':<20}{currency_symbol + tip:>10}"),
        (True, f"{'TOTAL':<20}{currency_symbol + total:>10}"),
        (False, ""),
        (False, "CARD ****0001  APPROVED"),
        (False, "NOT A REAL RECEIPT"),
    ]
    return lines


def degrade(image: Image.Image, rng: random.Random) -> Image.Image:
    """Blur, rotate and add noise, so the review path has something to catch."""
    noisy = image.rotate(rng.uniform(-2.2, 2.2), expand=True, fillcolor=255)
    noisy = noisy.filter(ImageFilter.GaussianBlur(radius=1.1))
    pixels = noisy.load()
    assert pixels is not None
    for _ in range(int(noisy.width * noisy.height * 0.02)):
        x = rng.randrange(noisy.width)
        y = rng.randrange(noisy.height)
        pixels[x, y] = rng.choice([0, 255])
    return noisy


def main() -> None:
    rng = random.Random(SEED)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    written: list[str] = []

    grocers = receipt_lines(
        "SANDBOX GROCERS",
        "08/14/2026  14:32",
        [("Oat Milk 1L", "4.99"), ("Sourdough Loaf", "6.50"),
         ("Coffee Beans 12oz", "14.25"), ("Bananas 2.1lb", "2.31")],
        "28.05", "2.31", "0.00", "30.36",
    )
    render(grocers).convert("RGB").save(OUTPUT_DIR / "receipt_grocers_synthetic.png")
    written.append("receipt_grocers_synthetic.png")

    cafe = receipt_lines(
        "SANDBOX COFFEE HOUSE",
        "08/16/2026  08:11",
        [("Flat White", "4.75"), ("Almond Croissant", "3.95")],
        "8.70", "0.72", "1.50", "10.92",
    )
    render(cafe).convert("RGB").save(
        OUTPUT_DIR / "receipt_cafe_synthetic.jpg", quality=92
    )
    written.append("receipt_cafe_synthetic.jpg")

    hardware = receipt_lines(
        "SANDBOX HARDWARE CO",
        "08/18/2026  16:45",
        [("Extension Cord", "18.99"), ("LED Bulbs 4pk", "12.49"),
         ("Duct Tape", "6.25")],
        "37.73", "3.11", "0.00", "40.84",
    )
    render(hardware).convert("RGB").save(OUTPUT_DIR / "receipt_hardware_synthetic.pdf")
    written.append("receipt_hardware_synthetic.pdf")

    # Non-base currency: exercises the review-time warning.
    euro = receipt_lines(
        "SANDBOX BOOKS EU",
        "08/20/2026  11:05",
        [("Notebook A5", "8.90"), ("Fountain Pen", "21.50")],
        "30.40", "2.51", "0.00", "32.91",
        currency_symbol="EUR ",
    )
    render(euro, width=520).convert("RGB").save(
        OUTPUT_DIR / "receipt_eur_synthetic.png"
    )
    written.append("receipt_eur_synthetic.png")

    # Deliberately hard to read.
    faded = receipt_lines(
        "SANDBOX DINER",
        "08/22/2026  19:20",
        [("Soup of the Day", "7.25"), ("Club Sandwich", "13.50")],
        "20.75", "1.71", "3.00", "25.46",
    )
    degrade(render(faded), rng).convert("RGB").save(
        OUTPUT_DIR / "receipt_faded_synthetic.png"
    )
    written.append("receipt_faded_synthetic.png")

    print(f"Wrote {len(written)} synthetic receipts to "
          f"{OUTPUT_DIR.relative_to(REPO_ROOT)}:")
    for name in written:
        print(f"  {name}")
    print("\nEvery receipt is fabricated and marked SYNTHETIC DEMO / NOT A REAL RECEIPT.")


if __name__ == "__main__":
    main()
