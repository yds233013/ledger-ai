"""Optional OpenAI integration.

Nothing in this package is required. With no API key the whole application
runs on its deterministic engines, and `get_ai_client()` returns None so no
component here is ever constructed.

The privacy contract is narrow and enforced by tests: only merchant *names*,
the plan schema, and already-computed result rows may leave the system. Raw
uploaded files, receipt images, OCR text, account identifiers and full
transaction histories never do.
"""

from .client import (
    AiClient,
    AiError,
    AiRateLimitError,
    AiTimeoutError,
    OpenAiClient,
    get_ai_client,
    reset_ai_client,
)

__all__ = [
    "AiClient",
    "AiError",
    "AiRateLimitError",
    "AiTimeoutError",
    "OpenAiClient",
    "get_ai_client",
    "reset_ai_client",
]
