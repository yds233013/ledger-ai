"""LLM categorizer.

Sends merchant *names* and nothing else — no amounts, no dates, no account
identifiers, no descriptions, no uploaded content. Results are cached per
normalized merchant, so any given merchant is sent at most once, ever.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal

from redis import Redis

from ...config import settings
from ..categorize.base import (
    CategorizationContext,
    CategorySource,
    CategorySuggestion,
    TransactionCandidate,
)
from ..categorize.rules import UNCATEGORIZED, RuleCategorizer
from .client import AiClient, AiError

logger = logging.getLogger(__name__)

CACHE_PREFIX = "merchant-category:"
CACHE_TTL_SECONDS = 60 * 60 * 24 * 30
CONFIDENCE_LLM = Decimal("0.80")

SYSTEM_PROMPT = """You assign a spending category to a merchant name.

Rules:
- Choose exactly one slug from the supplied list. Never invent one.
- If the merchant is unrecognizable, choose "uncategorized".
- Reply as JSON: {"category_slug": "..."}
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"category_slug": {"type": "string"}},
    "required": ["category_slug"],
}


class LLMCategorizer:
    """Deterministic stages first; the model only sees what they could not place.

    Wraps RuleCategorizer rather than replacing it, so correction memory and the
    seeded merchant patterns always win and the model is a last resort before
    the review queue.
    """

    name = "llm"

    def __init__(
        self,
        client: AiClient,
        allowed_slugs: list[str],
        redis: Redis | None = None,
        fallback: RuleCategorizer | None = None,
    ) -> None:
        self._client = client
        self._allowed = set(allowed_slugs)
        self._allowed_list = sorted(allowed_slugs)
        self._fallback = fallback or RuleCategorizer()
        self._redis = redis
        self._memo: dict[str, str] = {}

    def categorize(
        self, candidate: TransactionCandidate, context: CategorizationContext
    ) -> CategorySuggestion:
        deterministic = self._fallback.categorize(candidate, context)
        if deterministic.category_slug != UNCATEGORIZED:
            return deterministic

        slug = self._lookup(candidate.merchant_key)
        if slug is None:
            slug = self._ask(candidate)
            if slug is None:
                return deterministic
            self._remember(candidate.merchant_key, slug)

        if slug == UNCATEGORIZED or slug not in self._allowed:
            return deterministic

        return CategorySuggestion(
            category_slug=slug,
            confidence=CONFIDENCE_LLM,
            source=CategorySource.LLM,
            matched_on=f"a language model matched the merchant name “{candidate.merchant}”",
        )

    def _lookup(self, merchant_key: str) -> str | None:
        if merchant_key in self._memo:
            return self._memo[merchant_key]
        if self._redis is None:
            return None
        try:
            cached = self._redis.get(f"{CACHE_PREFIX}{merchant_key}")
        except Exception:  # noqa: BLE001 - a cache outage must not break imports
            return None
        if cached is None:
            return None
        value = cached.decode() if isinstance(cached, bytes) else str(cached)
        self._memo[merchant_key] = value
        return value

    def _remember(self, merchant_key: str, slug: str) -> None:
        self._memo[merchant_key] = slug
        if self._redis is None:
            return
        try:
            self._redis.setex(f"{CACHE_PREFIX}{merchant_key}", CACHE_TTL_SECONDS, slug)
        except Exception:  # noqa: BLE001
            return

    def _ask(self, candidate: TransactionCandidate) -> str | None:
        """The only outbound call. Merchant name only."""
        try:
            payload = self._client.complete_json(
                schema=RESPONSE_SCHEMA,
                schema_name="merchant_category",
                system=SYSTEM_PROMPT,
                user=json.dumps(
                    {
                        # redact_for_model is the single approved projection.
                        "merchant": candidate.merchant,
                        "allowed_category_slugs": self._allowed_list,
                    }
                ),
            )
        except AiError as exc:
            logger.info(
                "LLM categorizer unavailable (%s); leaving this merchant for review",
                type(exc).__name__,
            )
            return None

        slug = str(payload.get("category_slug", "")).strip().lower()
        if slug not in self._allowed:
            logger.info("LLM proposed an unknown category slug; ignoring it")
            return None
        return slug


def build_llm_categorizer(
    client: AiClient, allowed_slugs: list[str]
) -> LLMCategorizer:
    redis: Redis | None
    try:
        redis = Redis.from_url(settings.redis_url)
    except Exception:  # noqa: BLE001
        redis = None
    return LLMCategorizer(client=client, allowed_slugs=allowed_slugs, redis=redis)
