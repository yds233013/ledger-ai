"""Ask Ledger orchestration.

Emits the five inspectable steps as they genuinely complete, persists each one
to analysis_steps, and yields SSE-ready events. A cached answer replays the
persisted steps so the interface never has two different shapes.

Step order is the contract with the UI:
    understand -> select -> aggregate -> visualize -> explain
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...config import settings
from ...models import (
    AnalysisRun,
    AnalysisStatus,
    AnalysisStep,
    AnalysisStepName,
    NarratorKind,
    PlannerKind,
    StepStatus,
)
from ..ai import get_ai_client
from . import cache as cache_module
from .charts import ChartSpec, build_chart
from .executor import (
    ExecutionResult,
    count_matching,
    data_watermark,
    excluded_currency_counts,
    execute_plan,
    load_vocabulary,
)
from .narrate import (
    advice_response,
    build_narration,
    is_advice_request,
    verify_numeric_claims,
)
from .plan import AnalysisPlan
from .planner_rules import PlanExplanation, RulePlanner, UserVocabulary
from .refine import apply_refinement, available_refinements, top_row_label

logger = logging.getLogger(__name__)

STEP_TITLES = {
    AnalysisStepName.UNDERSTAND: "Understanding the question",
    AnalysisStepName.SELECT: "Selecting relevant transactions",
    AnalysisStepName.AGGREGATE: "Running a structured aggregation",
    AnalysisStepName.VISUALIZE: "Generating a chart",
    AnalysisStepName.EXPLAIN: "Preparing the explanation",
}


@dataclass(slots=True)
class StreamEvent:
    event: str
    data: dict[str, Any]


class AnalysisCancelledError(Exception):
    """The client disconnected mid-run."""


DisconnectCheck = Callable[[], Awaitable[bool]]


async def _never_disconnected() -> bool:
    return False


class AnalysisRunner:
    def __init__(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        base_currency: str = "USD",
    ) -> None:
        self._session = session
        self._user_id = user_id
        self._base_currency = base_currency.upper()
        self._planner = RulePlanner()
        self._seq = 0

    # -- step plumbing ----------------------------------------------------

    async def _emit(
        self,
        run: AnalysisRun,
        step: AnalysisStepName,
        status: StepStatus,
        title: str,
        payload: dict[str, Any],
        duration_ms: int = 0,
    ) -> StreamEvent:
        self._seq += 1
        record = AnalysisStep(
            run_id=run.id,
            seq=self._seq,
            step=step,
            status=status,
            title=title[:300],
            payload=payload,
            duration_ms=duration_ms,
        )
        self._session.add(record)
        await self._session.flush()
        return StreamEvent(
            event="step",
            data={
                "seq": self._seq,
                "step": step.value,
                "status": status.value,
                "title": title,
                "payload": payload,
                "duration_ms": duration_ms,
            },
        )

    async def _guard(self, is_disconnected: DisconnectCheck) -> None:
        if await is_disconnected():
            raise AnalysisCancelledError

    # -- main entry point --------------------------------------------------

    async def run(  # noqa: PLR0915 - the five steps are the readable unit here
        self,
        question: str,
        *,
        today: date | None = None,
        is_disconnected: DisconnectCheck | None = None,
        use_cache: bool = True,
        refine_from: uuid.UUID | None = None,
        refinement: str | None = None,
    ) -> AsyncIterator[StreamEvent]:
        disconnected = is_disconnected or _never_disconnected
        today = today or datetime.now(UTC).date()
        started = time.perf_counter()

        # A refinement is fully determined by (run_id, refinement key), so it
        # caches independently of the phrasing that produced the original run.
        cache_seed = (
            f"{question}|refine:{refine_from}:{refinement}"
            if refinement
            else question
        )
        normalized = cache_module.normalize_question(cache_seed)
        watermark = await data_watermark(self._session, self._user_id)
        cache_key = cache_module.build_cache_key(self._user_id, normalized, watermark)

        # --- cache replay -------------------------------------------------
        if use_cache:
            cached_id = await cache_module.lookup_run_id(cache_key)
            if cached_id:
                replay = await self._replay(cached_id)
                if replay is not None:
                    for event in replay:
                        yield event
                    return

        run = AnalysisRun(
            user_id=self._user_id,
            question=question[:2000],
            normalized_question=normalized[:2000],
            status=AnalysisStatus.RUNNING,
            planner=PlannerKind.RULES,
            narrator=NarratorKind.TEMPLATE,
            cache_key=cache_key,
        )
        self._session.add(run)
        await self._session.flush()

        yield StreamEvent(
            event="run",
            data={
                "run_id": str(run.id),
                "question": question,
                "cached": False,
                "ai_enabled": settings.ai_available,
            },
        )

        try:
            # --- 1. understand --------------------------------------------
            await self._guard(disconnected)
            tick = time.perf_counter()
            yield await self._emit(
                run, AnalysisStepName.UNDERSTAND, StepStatus.STARTED,
                STEP_TITLES[AnalysisStepName.UNDERSTAND], {},
            )

            if is_advice_request(question):
                async for event in self._decline_advice(run, question, started):
                    yield event
                return

            slugs, merchants, accounts = await load_vocabulary(self._session, self._user_id)
            vocab = UserVocabulary(
                category_slugs=slugs,
                merchants=merchants,
                account_names=accounts,
                base_currency=self._base_currency,
            )
            refined_from_question: str | None = None
            if refinement is not None and refine_from is not None:
                plan, explanation, refined_from_question = await self._refine(
                    refine_from, refinement, vocab
                )
                planner_kind, fallback_reason = PlannerKind.RULES, None
            else:
                plan, explanation, planner_kind, fallback_reason = self._plan(
                    question, vocab, today
                )
            run.planner = planner_kind
            run.plan = plan.model_dump(mode="json")

            yield await self._emit(
                run, AnalysisStepName.UNDERSTAND, StepStatus.COMPLETED,
                f"Interpreted as: {plan.describe()}",
                {
                    "planner": planner_kind.value,
                    "planner_note": (
                        "Parsed deterministically in application code — no language "
                        "model was involved in interpreting this question."
                        if planner_kind == PlannerKind.RULES
                        else "A language model proposed this plan. It was validated "
                        "against the same schema the deterministic planner uses, and "
                        "it computed nothing."
                    ),
                    "planner_fallback_reason": fallback_reason,
                    "refined_from": refined_from_question,
                    "interpretation": {
                        "intent": explanation.matched_intent,
                        "period": explanation.matched_period,
                        "filters": explanation.matched_filters,
                        "assumptions": explanation.assumptions,
                    },
                    "plan": plan.model_dump(mode="json"),
                },
                _ms(tick),
            )

            # --- 2. select -------------------------------------------------
            await self._guard(disconnected)
            tick = time.perf_counter()
            yield await self._emit(
                run, AnalysisStepName.SELECT, StepStatus.STARTED,
                STEP_TITLES[AnalysisStepName.SELECT], {},
            )
            matched, first, last, select_sql = await count_matching(
                self._session, self._user_id, plan
            )
            yield await self._emit(
                run, AnalysisStepName.SELECT, StepStatus.COMPLETED,
                f"Matched {matched} transaction{'s' if matched != 1 else ''}",
                {
                    "matched_transactions": matched,
                    "date_range": {
                        "start": plan.date_range.start.isoformat(),
                        "end": plan.date_range.end.isoformat(),
                        "label": plan.date_range.label,
                    },
                    "observed_range": {
                        "first": first.isoformat() if first else None,
                        "last": last.isoformat() if last else None,
                    },
                    "filters_applied": explanation.matched_filters,
                    "sql": select_sql,
                },
                _ms(tick),
            )

            # --- 3. aggregate ----------------------------------------------
            await self._guard(disconnected)
            tick = time.perf_counter()
            yield await self._emit(
                run, AnalysisStepName.AGGREGATE, StepStatus.STARTED,
                STEP_TITLES[AnalysisStepName.AGGREGATE], {},
            )
            result = await execute_plan(self._session, self._user_id, plan)

            # Say what a different currency put out of scope, rather than
            # letting the total quietly omit it.
            excluded = await excluded_currency_counts(self._session, self._user_id, plan)
            if excluded:
                summary = ", ".join(
                    f"{count} in {code}" for code, count in sorted(excluded.items())
                )
                noun = "transaction is" if sum(excluded.values()) == 1 else "transactions are"
                result.caveats.append(
                    f"{summary} {noun} not included. Ledger AI does not convert "
                    f"between currencies, so only {plan.currency} amounts are "
                    f"totalled here."
                )
            run.result = result.as_dict()
            yield await self._emit(
                run, AnalysisStepName.AGGREGATE, StepStatus.COMPLETED,
                f"Computed {result.metric_label.lower()} over {result.transaction_count} rows",
                {
                    "computation": result.sql_description,
                    "computed_by": (
                        "PostgreSQL aggregate over your transactions. Every figure below "
                        "was calculated by the database, not written by a model."
                    ),
                    "sql": result.sql_text,
                    "result": result.as_dict(),
                    "supporting_transactions": result.supporting,
                },
                _ms(tick),
            )

            # --- 4. visualize ----------------------------------------------
            await self._guard(disconnected)
            tick = time.perf_counter()
            yield await self._emit(
                run, AnalysisStepName.VISUALIZE, StepStatus.STARTED,
                STEP_TITLES[AnalysisStepName.VISUALIZE], {},
            )
            chart: ChartSpec = build_chart(plan, result)
            run.chart_spec = chart.as_dict()
            yield await self._emit(
                run, AnalysisStepName.VISUALIZE, StepStatus.COMPLETED,
                (
                    f"Built a {chart.kind} chart with {len(chart.data)} data points"
                    if chart.kind != "none"
                    else "No chart — this answer is a single figure"
                ),
                {"chart": chart.as_dict()},
                _ms(tick),
            )

            # --- 5. explain -------------------------------------------------
            await self._guard(disconnected)
            tick = time.perf_counter()
            yield await self._emit(
                run, AnalysisStepName.EXPLAIN, StepStatus.STARTED,
                STEP_TITLES[AnalysisStepName.EXPLAIN], {},
            )
            narration, narrator, verification = self._narrate(plan, result, slugs)
            run.narration = narration
            run.narrator = narrator
            yield await self._emit(
                run, AnalysisStepName.EXPLAIN, StepStatus.COMPLETED,
                "Explanation prepared",
                {
                    "narrator": narrator.value,
                    "narrator_note": (
                        "Written from a fixed template using only the computed figures "
                        "above. No API key is configured, so no model was called."
                        if narrator == NarratorKind.TEMPLATE
                        else "Model-written wording, checked against the computed figures."
                    ),
                    "numeric_verification": verification,
                    "narration": narration,
                    "caveats": result.caveats,
                },
                _ms(tick),
            )

            # --- finish -----------------------------------------------------
            run.status = AnalysisStatus.COMPLETE
            run.duration_ms = _ms(started)
            await self._session.commit()
            await cache_module.store_run_id(cache_key, run.id, self._user_id)

            payload = self._final_payload(run, result, chart, narration, cached=False)
            payload["refinements"] = available_refinements(
                plan, top_row_label(result.as_dict()["rows"])
            )
            payload["refined_from"] = refined_from_question
            yield StreamEvent(event="result", data=payload)

        except AnalysisCancelledError:
            run.status = AnalysisStatus.CANCELLED
            run.duration_ms = _ms(started)
            await self._session.commit()
            yield StreamEvent(event="cancelled", data={"run_id": str(run.id)})

        except Exception as exc:  # noqa: BLE001 - every failure must reach the client
            logger.exception("Analysis failed for user %s", self._user_id)
            run.status = AnalysisStatus.FAILED
            run.error_message = f"{type(exc).__name__}: {exc}"[:500]
            run.duration_ms = _ms(started)
            await self._session.commit()
            yield StreamEvent(
                event="error",
                data={
                    "run_id": str(run.id),
                    "message": (
                        "Something went wrong while analysing that question. "
                        "You can retry, or try rephrasing it."
                    ),
                    "retryable": True,
                },
            )

    # -- helpers -----------------------------------------------------------

    def _plan(
        self, question: str, vocab: UserVocabulary, today: date
    ) -> tuple[AnalysisPlan, PlanExplanation, PlannerKind, str | None]:
        """Plan the question, preferring the model only when it validates."""
        client = get_ai_client()
        if client is not None:
            from ..ai.planner_llm import LLMPlanner

            return LLMPlanner(client, self._planner).plan(question, vocab, today)

        plan, explanation = self._planner.plan(question, vocab, today)
        return plan, explanation, PlannerKind.RULES, None

    def _narrate(
        self, plan: AnalysisPlan, result: ExecutionResult, category_names: dict[str, str]
    ) -> tuple[str, NarratorKind, dict[str, Any]]:
        """Template narration by default; model wording only when it verifies."""
        client = get_ai_client()
        if client is not None:
            from ..ai.narrator_llm import narrate_with_model

            return narrate_with_model(client, plan, result, category_names)

        narration = build_narration(plan, result, category_names)
        verified, unverified = verify_numeric_claims(narration, result)
        return (
            narration,
            NarratorKind.TEMPLATE,
            {
                "checked": True,
                "passed": verified,
                "unverified_numbers": unverified,
                "note": (
                    "Every number in the explanation was matched against the computed "
                    "result set before being shown."
                ),
            },
        )

    async def _decline_advice(
        self, run: AnalysisRun, question: str, started: float
    ) -> AsyncIterator[StreamEvent]:
        message = advice_response(question)
        run.status = AnalysisStatus.COMPLETE
        run.narration = message
        run.result = {"declined": True}
        run.duration_ms = _ms(started)
        yield await self._emit(
            run, AnalysisStepName.UNDERSTAND, StepStatus.COMPLETED,
            "This question asks for financial advice",
            {
                "declined": True,
                "reason": (
                    "Ledger AI reports on your uploaded data. It does not provide "
                    "financial advice or recommendations."
                ),
            },
        )
        await self._session.commit()
        yield StreamEvent(
            event="result",
            data={
                "run_id": str(run.id),
                "declined": True,
                "narration": message,
                "chart": None,
                "result": None,
                "supporting_transactions": [],
                "cached": False,
            },
        )

    def _final_payload(
        self,
        run: AnalysisRun,
        result: ExecutionResult,
        chart: ChartSpec,
        narration: str,
        *,
        cached: bool,
    ) -> dict[str, Any]:
        return {
            "run_id": str(run.id),
            "question": run.question,
            "plan": run.plan,
            "result": result.as_dict(),
            "chart": chart.as_dict(),
            "narration": narration,
            "supporting_transactions": result.supporting,
            "caveats": result.caveats,
            "planner": run.planner.value if hasattr(run.planner, "value") else run.planner,
            "narrator": run.narrator.value if hasattr(run.narrator, "value") else run.narrator,
            "duration_ms": run.duration_ms,
            "cached": cached,
            "declined": False,
        }

    async def _refine(
        self, run_id: uuid.UUID, refinement: str, vocab: UserVocabulary
    ) -> tuple[AnalysisPlan, PlanExplanation, str]:
        """Apply a named refinement to a previous run's validated plan.

        The source run is loaded scoped to this user, so a refinement cannot
        reach anyone else's analysis.
        """
        source = (
            await self._session.execute(
                select(AnalysisRun).where(
                    AnalysisRun.id == run_id,
                    AnalysisRun.user_id == self._user_id,
                    AnalysisRun.status == AnalysisStatus.COMPLETE,
                )
            )
        ).scalar_one_or_none()
        if source is None or source.plan is None:
            raise ValueError("That analysis is no longer available to refine.")

        previous = AnalysisPlan.model_validate(source.plan)
        try:
            refined, label = apply_refinement(
                previous, refinement, vocab.category_slugs, vocab.merchants
            )
        except KeyError as exc:
            raise ValueError(f"Unknown refinement: {refinement}") from exc

        explanation = PlanExplanation(
            matched_intent=f"{refined.intent.value} (refined: {label.lower()})",
            matched_period=(
                f"{refined.date_range.label} "
                f"({refined.date_range.start} to {refined.date_range.end})"
            ),
            matched_filters=_describe_plan_filters(refined, vocab),
            assumptions=[
                f"Refined from your earlier question: “{source.question}”.",
                "A refinement is a fixed transformation of the previous plan — "
                "nothing was inferred from conversation history.",
            ],
        )
        return refined, explanation, source.question

    async def _replay(self, run_id: str) -> list[StreamEvent] | None:
        """Re-emit a previous run's persisted steps.

        Scoped to the current user: a cache key collision across users could
        otherwise leak an answer. The user_id predicate makes that impossible.
        """
        try:
            run = (
                await self._session.execute(
                    select(AnalysisRun)
                    .options(selectinload(AnalysisRun.steps))
                    .where(
                        AnalysisRun.id == uuid.UUID(run_id),
                        AnalysisRun.user_id == self._user_id,
                        AnalysisRun.status == AnalysisStatus.COMPLETE,
                    )
                )
            ).scalar_one_or_none()
        except ValueError:
            return None

        if run is None or run.result is None:
            return None

        events: list[StreamEvent] = [
            StreamEvent(
                event="run",
                data={
                    "run_id": str(run.id),
                    "question": run.question,
                    "cached": True,
                    "ai_enabled": settings.ai_available,
                },
            )
        ]
        events += [
            StreamEvent(
                event="step",
                data={
                    "seq": step.seq,
                    "step": step.step.value if hasattr(step.step, "value") else step.step,
                    "status": (
                        step.status.value if hasattr(step.status, "value") else step.status
                    ),
                    "title": step.title,
                    "payload": step.payload,
                    "duration_ms": step.duration_ms,
                },
            )
            for step in run.steps
        ]
        # A cached answer must offer the same follow-ups a live one does, or the
        # UI silently loses a feature whenever a question repeats.
        cached_refinements: list[dict[str, Any]] = []
        if run.plan:
            try:
                cached_plan = AnalysisPlan.model_validate(run.plan)
                cached_refinements = available_refinements(
                    cached_plan, top_row_label((run.result or {}).get("rows", []))
                )
            except ValidationError:
                cached_refinements = []

        events.append(
            StreamEvent(
                event="result",
                data={
                    "run_id": str(run.id),
                    "question": run.question,
                    "plan": run.plan,
                    "refinements": cached_refinements,
                    "refined_from": None,
                    "result": run.result,
                    "chart": run.chart_spec,
                    "narration": run.narration,
                    "supporting_transactions": _supporting_from_steps(run.steps),
                    "caveats": (run.result or {}).get("caveats", []),
                    "planner": (
                        run.planner.value if hasattr(run.planner, "value") else run.planner
                    ),
                    "narrator": (
                        run.narrator.value if hasattr(run.narrator, "value") else run.narrator
                    ),
                    "duration_ms": run.duration_ms,
                    "cached": True,
                    "declined": bool((run.result or {}).get("declined")),
                },
            )
        )
        return events


def _describe_plan_filters(plan: AnalysisPlan, vocab: UserVocabulary) -> list[str]:
    described = [
        f"category: {vocab.category_slugs.get(slug, slug)}"
        for slug in plan.filters.category_slugs
    ]
    described += [f"merchant: {name}" for name in plan.filters.merchants]
    if plan.filters.text_query:
        described.append(f"description contains: “{plan.filters.text_query}”")
    return described or ["none — all transactions in range"]


def _supporting_from_steps(steps: list[AnalysisStep]) -> list[dict[str, Any]]:
    for step in steps:
        rows = (step.payload or {}).get("supporting_transactions")
        if rows:
            return rows
    return []


def _ms(since: float) -> int:
    return max(0, int((time.perf_counter() - since) * 1000))
