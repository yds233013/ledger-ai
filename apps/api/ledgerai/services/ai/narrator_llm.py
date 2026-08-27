"""LLM narrator.

Receives the plan and the already-computed result rows — never the database,
never a file, never a receipt. Whatever it writes is put through the same
numeric guard the template narration passes: any figure that is not in the
computed result set causes the narration to be discarded entirely.
"""

from __future__ import annotations

import json
import logging

from ...models import NarratorKind
from ..analysis.executor import ExecutionResult
from ..analysis.narrate import build_narration, verify_numeric_claims
from ..analysis.plan import AnalysisPlan
from .client import AiClient, AiError

logger = logging.getLogger(__name__)

MAX_NARRATION_CHARS = 700

SYSTEM_PROMPT = """You write one short, plain-language explanation of a
personal-finance result that has already been computed.

Rules:
- Use ONLY the numbers given to you. Never compute, estimate, or infer a figure.
- Do not give financial advice or recommendations of any kind.
- Two or three sentences. No lists, no headings, no markdown.
- Reply as JSON: {"explanation": "..."}
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"explanation": {"type": "string"}},
    "required": ["explanation"],
}


def narrate_with_model(
    client: AiClient,
    plan: AnalysisPlan,
    result: ExecutionResult,
    category_names: dict[str, str],
) -> tuple[str, NarratorKind, dict]:
    """Return (narration, which narrator produced it, verification payload).

    Falls back to the deterministic template on any failure *or* on any
    unverifiable number.
    """
    template = build_narration(plan, result, category_names)

    def deterministic(reason: str) -> tuple[str, NarratorKind, dict]:
        verified, unverified = verify_numeric_claims(template, result)
        return (
            template,
            NarratorKind.TEMPLATE,
            {
                "checked": True,
                "passed": verified,
                "unverified_numbers": unverified,
                "fallback_reason": reason,
                "note": (
                    "Every number in the explanation was matched against the "
                    "computed result set before being shown."
                ),
            },
        )

    try:
        payload = client.complete_json(
            schema=RESPONSE_SCHEMA,
            schema_name="explanation",
            system=SYSTEM_PROMPT,
            user=_build_prompt(plan, result),
        )
    except AiError as exc:
        logger.info("LLM narrator unavailable (%s); using the template", type(exc).__name__)
        return deterministic(f"the model call failed ({type(exc).__name__})")

    narration = str(payload.get("explanation", "")).strip()
    if not narration or len(narration) > MAX_NARRATION_CHARS:
        return deterministic("the model returned an empty or over-long explanation")

    verified, unverified = verify_numeric_claims(narration, result)
    if not verified:
        # This is the guarantee: a model-written number that the computation
        # never produced never reaches the user.
        logger.warning("Discarding model narration: %d unverifiable number(s)", len(unverified))
        return deterministic(
            f"the model's wording contained {len(unverified)} number(s) that were "
            "not in the computed result"
        )

    return (
        narration,
        NarratorKind.LLM,
        {
            "checked": True,
            "passed": True,
            "unverified_numbers": [],
            "note": (
                "A language model wrote this wording. Every number in it was "
                "matched against the SQL-computed result before being shown."
            ),
        },
    )


def _build_prompt(plan: AnalysisPlan, result: ExecutionResult) -> str:
    """Only computed output. No raw transactions beyond the supporting rows the
    user can already see, and no file or receipt content of any kind."""
    return json.dumps(
        {
            "question_intent": plan.intent.value,
            "period": plan.date_range.label,
            "currency": plan.currency,
            "computed": result.as_dict(),
        },
        default=str,
    )
