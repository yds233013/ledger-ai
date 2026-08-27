"""The AI client boundary.

`AiClient` is a Protocol, so every test injects a fake and the suite never
touches the network or needs a key. `OpenAiClient` is the only implementation
that talks to OpenAI, and it is constructed solely by `get_ai_client()` when a
key is actually configured.

A small circuit breaker keeps a degraded API from slowing every request: after
a few consecutive failures the client stops trying for a cooldown and callers
fall straight through to their deterministic path.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Protocol

from ...config import settings

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT_SECONDS = 8.0
MAX_ATTEMPTS = 2
BREAKER_THRESHOLD = 3
BREAKER_COOLDOWN_SECONDS = 60.0


class AiError(Exception):
    """Any AI failure. Callers catch this and fall back deterministically."""


class AiTimeoutError(AiError):
    pass


class AiRateLimitError(AiError):
    pass


class AiUnavailableError(AiError):
    """The breaker is open, or no client is configured."""


class AiClient(Protocol):
    name: str

    def complete_json(
        self,
        *,
        schema: dict[str, Any],
        schema_name: str,
        system: str,
        user: str,
    ) -> dict[str, Any]: ...


class _CircuitBreaker:
    """Stops hammering an API that is clearly unwell."""

    def __init__(self) -> None:
        self._failures = 0
        self._opened_at: float | None = None

    def check(self) -> None:
        if self._opened_at is None:
            return
        if time.monotonic() - self._opened_at < BREAKER_COOLDOWN_SECONDS:
            raise AiUnavailableError(
                "AI is temporarily disabled after repeated failures."
            )
        # Cooldown elapsed: allow one probe.
        self._opened_at = None
        self._failures = 0

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= BREAKER_THRESHOLD:
            self._opened_at = time.monotonic()
            logger.warning(
                "AI circuit breaker opened after %d consecutive failures", self._failures
            )


class OpenAiClient:
    """Structured-output JSON completions.

    Never constructed unless `settings.ai_available` is true, which requires
    both the feature flag and a non-empty key.
    """

    name = "openai"

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        from openai import OpenAI

        self._client = OpenAI(
            api_key=api_key or settings.openai_api_key,
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=0,  # retries are handled here, with the breaker
        )
        self._model = model or settings.openai_model
        self._breaker = _CircuitBreaker()

    def complete_json(
        self,
        *,
        schema: dict[str, Any],
        schema_name: str,
        system: str,
        user: str,
    ) -> dict[str, Any]:
        from openai import APIError, APITimeoutError, RateLimitError

        self._breaker.check()
        last_error: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            try:
                response = self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    response_format={
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema_name,
                            "schema": schema,
                            "strict": False,
                        },
                    },
                    temperature=0,
                )
                content = response.choices[0].message.content or ""
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise AiError("Model returned JSON that is not an object")
                self._breaker.record_success()
                return parsed

            except APITimeoutError as exc:
                last_error = AiTimeoutError(str(exc))
            except RateLimitError as exc:
                last_error = AiRateLimitError(str(exc))
                break  # retrying a rate limit immediately is pointless
            except json.JSONDecodeError as exc:
                last_error = AiError(f"Model returned malformed JSON: {exc}")
            except APIError as exc:
                last_error = AiError(f"OpenAI API error: {exc}")
            except Exception as exc:  # noqa: BLE001 - never surface to the user
                last_error = AiError(f"Unexpected AI failure: {type(exc).__name__}")

            if attempt < MAX_ATTEMPTS - 1:
                logger.info("AI attempt %d failed; retrying", attempt + 1)

        self._breaker.record_failure()
        raise last_error if isinstance(last_error, AiError) else AiError("AI call failed")


_client: AiClient | None = None
_resolved = False


def get_ai_client() -> AiClient | None:
    """The active AI client, or None when AI is not configured.

    Returning None rather than raising is deliberate: every caller already has
    a deterministic path, and "no AI" is a normal operating mode, not an error.
    """
    global _client, _resolved
    if _resolved:
        return _client

    _resolved = True
    if not settings.ai_available:
        _client = None
        return None

    try:
        _client = OpenAiClient()
        logger.info("AI enabled using model %s", settings.openai_model)
    except Exception:  # noqa: BLE001 - a broken client must not break startup
        logger.exception("Could not construct the AI client; continuing without AI")
        _client = None
    return _client


def reset_ai_client(client: AiClient | None = None) -> None:
    """Inject a client (tests) or clear the cached one."""
    global _client, _resolved
    _client = client
    _resolved = client is not None
