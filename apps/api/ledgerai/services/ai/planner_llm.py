"""LLM question planner.

The model proposes an AnalysisPlan; it never writes SQL and never produces a
figure. Whatever comes back is validated by the same Pydantic model the
deterministic planner produces, so a hallucinated field or an impossible
combination fails loudly and the RulePlanner answer is used instead.

What the model is sent: the plan schema, today's date, and the user's distinct
category and merchant *names*. Never an amount, a date on a transaction, an
account identifier, an uploaded file, or any receipt text.
"""

from __future__ import annotations

import json
import logging
from datetime import date

from pydantic import ValidationError

from ...models import PlannerKind
from ..analysis.plan import AnalysisPlan
from ..analysis.planner_rules import PlanExplanation, RulePlanner, UserVocabulary
from .client import AiClient, AiError

logger = logging.getLogger(__name__)

MAX_VOCABULARY_TERMS = 120

SYSTEM_PROMPT = """You convert a personal-finance question into a query plan.

Rules:
- Reply with JSON matching the provided schema. Nothing else.
- Never compute or state a number. You are choosing filters, not answering.
- Resolve relative dates against the supplied today's date into absolute dates.
- Only use category slugs and merchant names from the supplied vocabulary.
- If the question is ambiguous, choose the simplest reasonable plan.
"""


class LLMPlanner:
    """Wraps the rules planner and prefers the model's plan when it validates."""

    name = "llm"

    def __init__(self, client: AiClient, fallback: RulePlanner | None = None) -> None:
        self._client = client
        self._fallback = fallback or RulePlanner()

    def plan(
        self, question: str, vocab: UserVocabulary, today: date
    ) -> tuple[AnalysisPlan, PlanExplanation, PlannerKind, str | None]:
        """Return (plan, explanation, which planner produced it, fallback reason)."""
        rules_plan, rules_explanation = self._fallback.plan(question, vocab, today)

        try:
            payload = self._client.complete_json(
                schema=_plan_schema(),
                schema_name="analysis_plan",
                system=SYSTEM_PROMPT,
                user=_build_user_prompt(question, vocab, today),
            )
        except AiError as exc:
            # Timeout, rate limit, malformed JSON, breaker open — all the same
            # to the caller: use the deterministic plan.
            logger.info("LLM planner unavailable (%s); using the rules planner", type(exc).__name__)
            return rules_plan, rules_explanation, PlannerKind.RULES, _reason(exc)

        try:
            # The model's output must satisfy exactly the same contract the
            # deterministic planner satisfies. Nothing gets a looser check.
            candidate = AnalysisPlan.model_validate(payload)
        except ValidationError as exc:
            logger.info("LLM plan failed validation with %d error(s)", len(exc.errors()))
            return (
                rules_plan,
                rules_explanation,
                PlannerKind.RULES,
                f"the model's plan failed validation ({len(exc.errors())} problem(s))",
            )

        # The model may not choose the currency: mixing currencies is not a
        # decision that is available to anyone.
        candidate = candidate.model_copy(update={"currency": rules_plan.currency})

        explanation = PlanExplanation(
            matched_intent=f"{candidate.intent.value} (proposed by the language model)",
            matched_period=(
                f"{candidate.date_range.label} "
                f"({candidate.date_range.start} to {candidate.date_range.end})"
            ),
            matched_filters=_describe_filters(candidate, vocab),
            assumptions=[
                "A language model chose the filters and time period for this "
                "question. Every figure below is still computed by SQL over your "
                "own data.",
            ],
        )
        return candidate, explanation, PlannerKind.LLM, None


def _reason(exc: AiError) -> str:
    return {
        "AiTimeoutError": "the model did not respond in time",
        "AiRateLimitError": "the model API is rate limited",
        "AiUnavailableError": "the model is temporarily disabled after repeated failures",
    }.get(type(exc).__name__, "the model call failed")


def _plan_schema() -> dict:
    schema = AnalysisPlan.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def _build_user_prompt(question: str, vocab: UserVocabulary, today: date) -> str:
    """Only names and the question. No amounts, no dates, no identifiers."""
    categories = sorted(vocab.category_slugs)[:MAX_VOCABULARY_TERMS]
    merchants = vocab.merchants[:MAX_VOCABULARY_TERMS]
    return json.dumps(
        {
            "question": question,
            "today": today.isoformat(),
            "available_category_slugs": categories,
            "available_merchants": merchants,
        }
    )


def _describe_filters(plan: AnalysisPlan, vocab: UserVocabulary) -> list[str]:
    described = [
        f"category: {vocab.category_slugs.get(slug, slug)}"
        for slug in plan.filters.category_slugs
    ]
    described += [f"merchant: {name}" for name in plan.filters.merchants]
    if plan.filters.text_query:
        described.append(f"description contains: “{plan.filters.text_query}”")
    return described or ["none — all transactions in range"]
