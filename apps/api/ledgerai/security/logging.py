"""Log hygiene.

Ledger AI handles financial descriptions, OCR text and bearer tokens. None of
it belongs in a log line, and the defence is layered rather than trusted to
discipline alone:

  1. Call sites log identifiers, not content — the existing convention.
  2. This filter redacts anything that slips through, including from libraries
     whose formatting we do not control.
  3. Request access logging is disabled in production, because a URL like
     /api/transactions?search=Blue+Bottle+Coffee puts a merchant name in the
     log purely by existing.

The filter is a safety net, not permission to be careless.
"""

from __future__ import annotations

import logging
import re

# Order matters: the more specific patterns run first.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Bearer tokens and JWTs anywhere in a message.
    (re.compile(r"\bBearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE), "Bearer [REDACTED]"),
    (re.compile(r"\beyJ[A-Za-z0-9._-]{20,}"), "[REDACTED-JWT]"),
    # API keys.
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"), "[REDACTED-KEY]"),
    # Connection strings with credentials.
    (
        re.compile(r"\b([a-z+]+://)[^:/\s]+:[^@/\s]+@"),
        r"\1[REDACTED]:[REDACTED]@",
    ),
    # Query strings that carry search terms — merchant names are user data.
    (re.compile(r"([?&](?:search|merchant|q)=)[^\s&\"']+"), r"\1[REDACTED]"),
    # Anything explicitly marked by a caller.
    (re.compile(r"<redact>.*?</redact>", re.DOTALL), "[REDACTED]"),
]


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Scrub sensitive substrings from every record that passes through."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a broken record must not break logging
            return True

        cleaned = redact(message)
        if cleaned != message:
            # Replace the formatted message wholesale; args are already folded
            # in, so clearing them prevents a second interpolation.
            record.msg = cleaned
            record.args = ()
        return True


def _already_installed(target: logging.Logger | logging.Handler) -> bool:
    return any(isinstance(existing, RedactingFilter) for existing in target.filters)


def install_redaction() -> None:
    """Attach the filter to the root logger and to uvicorn's own loggers.

    Handler-level rather than logger-level, so records propagating from any
    library still pass through it. Idempotent: reloading a module or calling
    this twice must not stack duplicate filters on the same handler.
    """
    root = logging.getLogger()
    for handler in root.handlers:
        if not _already_installed(handler):
            handler.addFilter(RedactingFilter())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy.engine"):
        logger = logging.getLogger(name)
        if not _already_installed(logger):
            logger.addFilter(RedactingFilter())
        for handler in logger.handlers:
            if not _already_installed(handler):
                handler.addFilter(RedactingFilter())
