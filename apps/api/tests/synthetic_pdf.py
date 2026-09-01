"""A tiny PDF writer, so statement fixtures are generated and never collected.

Hand-written rather than a library: it adds no dependency, it is deterministic,
and — the reason that matters most — it gives exact control over where each run
of text sits. Column geometry is the thing under test, so the fixtures have to
be able to state their geometry precisely, including the pathological cases a
real bank would never produce.

Every page is stamped SYNTHETIC. A test asserts that stamp is present in every
generated fixture, so a real statement cannot be committed into the suite by
accident.
"""

from __future__ import annotations

from dataclasses import dataclass

PAGE_WIDTH = 612
PAGE_HEIGHT = 792

# Column positions used by the default layout, in PDF user space.
X_DATE = 58
X_DESC = 130
X_AMOUNT_RIGHT = 500
X_BALANCE_RIGHT = 580

STAMP = "*** SYNTHETIC — NOT A REAL STATEMENT ***"


@dataclass(frozen=True)
class Run:
    """One piece of text at one position."""

    x: float
    y: float
    text: str
    size: float = 9.5
    invisible: bool = False
    right_align_at: float | None = None
    # Fill colour as RGB in 0..1. None keeps the page default (black). White is
    # how a fixture hides a value in plain sight: the glyphs are drawn, the text
    # layer carries them, and the page shows nothing where they are.
    colour: tuple[float, float, float] | None = None

    def x0(self) -> float:
        if self.right_align_at is None:
            return self.x
        # Helvetica digits are ~0.556 em; close enough to right-align fixtures.
        return self.right_align_at - len(self.text) * self.size * 0.556


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def write_pdf(pages: list[list[Run]]) -> bytes:
    """Serialise pages of positioned runs into an uncompressed PDF."""
    objects: list[bytes] = []
    kids = " ".join(f"{4 + i * 2} 0 R" for i in range(len(pages)))
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode()
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for runs in pages:
        parts: list[str] = []
        for run in runs:
            mode = "3 Tr " if run.invisible else "0 Tr "
            # Always stated, never inherited. Fill colour is graphics state that
            # survives BT/ET, so one white run would otherwise silently paint
            # every run after it white too.
            red, green, blue = run.colour if run.colour is not None else (0.0, 0.0, 0.0)
            fill = f"{red} {green} {blue} rg "
            parts.append(
                f"BT {fill}{mode}/F1 {run.size} Tf {run.x0():.2f} {run.y:.2f} Td "
                f"({_escape(run.text)}) Tj ET"
            )
        stream = "\n".join(parts).encode()
        content_index = len(objects) + 2
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_index} 0 R >>".encode()
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"

    xref_at = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_at}\n%%EOF\n"
    ).encode()
    return bytes(out)


def _money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    whole, frac = divmod(abs(cents), 100)
    return f"{sign}{whole:,}.{frac:02d}"


def header_runs(
    *, period: str = "1 August 2026 to 31 August 2026", currency: str = "£"
) -> list[Run]:
    return [
        Run(58, 750, "SANDBOX RETAIL BANK", size=13),
        Run(58, 734, STAMP, size=8),
        Run(58, 718, f"Statement period: {period}", size=9),
        Run(58, 706, f"Account: ****4821    Currency: {currency}", size=9),
    ]


def transaction_page(
    rows: list[tuple[str, str, int, int | None]],
    *,
    top: float = 680,
    leading: float = 15,
    with_header: bool = True,
    amount_right: float = X_AMOUNT_RIGHT,
    balance_right: float | None = X_BALANCE_RIGHT,
    date_x: float = X_DATE,
    desc_x: float = X_DESC,
    invisible_extra: tuple[float, int] | None = None,
) -> list[Run]:
    """One page of transactions.

    Each row is (date_text, description, amount_cents, balance_cents|None).
    `invisible_extra` adds a money token to the text layer that is never drawn,
    which is how the tampered-file fixture is built.
    """
    runs = header_runs() if with_header else [Run(58, 750, STAMP, size=8)]
    y = top
    for date_text, description, amount, balance in rows:
        runs.append(Run(date_x, y, date_text))
        runs.append(Run(desc_x, y, description))
        runs.append(Run(0, y, _money(amount), right_align_at=amount_right))
        if balance is not None and balance_right is not None:
            runs.append(Run(0, y, _money(balance), right_align_at=balance_right))
        y -= leading

    if invisible_extra is not None:
        row_y, cents = invisible_extra
        runs.append(
            Run(0, row_y, _money(cents), right_align_at=amount_right, invisible=True)
        )
    return runs


def summary_page() -> list[Run]:
    """A page with no transaction table — the kind that must yield no rows."""
    return [
        Run(58, 750, "SANDBOX RETAIL BANK", size=13),
        Run(58, 734, STAMP, size=8),
        Run(58, 700, "Summary of your account", size=11),
        Run(58, 680, "Money in this period", size=9),
        Run(0, 680, "1,800.00", right_align_at=X_AMOUNT_RIGHT),
        Run(58, 664, "Money out this period", size=9),
        Run(0, 664, "81.96", right_align_at=X_AMOUNT_RIGHT),
        Run(58, 620, "Ways to bank with us: visit any branch.", size=9),
        Run(58, 604, "Your deposits are protected up to the statutory limit.", size=9),
    ]


DEFAULT_ROWS = [
    ("12 Aug", "SANDBOX GROCERS 0042", -4210, 190455),
    ("13 Aug", "SANDBOX TRANSIT AUTHORITY", -275, 190180),
    ("14 Aug", "SANDBOX GROCERS 0042", -3036, 187144),
    ("15 Aug", "SANDBOX COFFEE BAR", -675, 186469),
    ("16 Aug", "SANDBOX PAYROLL DEPOSIT", 180000, 366469),
]


def simple_statement() -> bytes:
    """One page, five rows, balance column present — the happy path."""
    return write_pdf([transaction_page(DEFAULT_ROWS)])


WHITE = (1.0, 1.0, 1.0)


def white_amount_statement() -> bytes:
    """One amount painted white-on-white; the rest of its row renders normally.

    The attack that a row-level liveness test cannot see. The date, description
    and balance on that row are all drawn and all legible, so the row looks
    entirely healthy — only the pixels inside the amount's own box are empty.
    """
    runs = header_runs()
    y = 680
    for index, (date_text, description, amount, balance) in enumerate(DEFAULT_ROWS):
        runs.append(Run(X_DATE, y, date_text))
        runs.append(Run(X_DESC, y, description))
        runs.append(
            Run(
                0,
                y,
                _money(amount),
                right_align_at=X_AMOUNT_RIGHT,
                colour=WHITE if index == 2 else None,
            )
        )
        if balance is not None:
            runs.append(Run(0, y, _money(balance), right_align_at=X_BALANCE_RIGHT))
        y -= 15
    return write_pdf([runs])


def transposed_without_balance_statement() -> bytes:
    """Two amounts swapped between rows, on a statement with no balance column.

    The page renders row A's amount on row B and vice versa, while the text
    layer claims the original order. Every value still present, every total
    unchanged, and no balance chain to appeal to — so only comparing each
    claimed token against the pixels at its own position can see it.
    """
    rows = [
        ("12 Aug", "SANDBOX GROCERS 0042", -4210),
        ("13 Aug", "SANDBOX TRANSIT AUTHORITY", -275),
        ("14 Aug", "SANDBOX HARDWARE DEPOT", -8899),
        ("15 Aug", "SANDBOX COFFEE BAR", -675),
        ("16 Aug", "SANDBOX BOOKSHOP", -1540),
    ]
    first, second = 0, 2
    runs = header_runs()
    positions: dict[int, float] = {}
    y = 680
    for index in range(len(rows)):
        positions[index] = y
        y -= 15

    for index, (date_text, description, amount) in enumerate(rows):
        row_y = positions[index]
        runs.append(Run(X_DATE, row_y, date_text))
        runs.append(Run(X_DESC, row_y, description))
        if index in (first, second):
            continue
        runs.append(Run(0, row_y, _money(amount), right_align_at=X_AMOUNT_RIGHT))

    shown_first, shown_second = _money(rows[second][2]), _money(rows[first][2])
    claimed_first, claimed_second = _money(rows[first][2]), _money(rows[second][2])
    # Drawn: the swapped order.
    runs.append(Run(0, positions[first], shown_first, right_align_at=X_AMOUNT_RIGHT))
    runs.append(Run(0, positions[second], shown_second, right_align_at=X_AMOUNT_RIGHT))
    # Claimed: the original order, invisible so only extraction sees it.
    runs.append(
        Run(0, positions[first], claimed_first, right_align_at=X_AMOUNT_RIGHT, invisible=True)
    )
    runs.append(
        Run(0, positions[second], claimed_second, right_align_at=X_AMOUNT_RIGHT, invisible=True)
    )
    return write_pdf([runs])
