"""Ask Ledger endpoints.

The stream is POST + `fetch`/ReadableStream rather than EventSource, because
EventSource cannot send a request body or an Authorization header — the
workarounds (token in the query string, a pre-created run id) are worse than
reading the stream manually on the client. AbortController then gives us
cancellation for free.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select
from sqlalchemy.orm import selectinload

from ..config import settings
from ..deps import CurrentUser, DbSession
from ..models import AnalysisRun, AnalysisStatus
from ..security.ratelimit import ANALYSIS_LIMIT, enforce
from ..services.analysis.runner import AnalysisRunner
from ..services.scoping import user_analysis_runs

router = APIRouter(prefix="/api/analysis", tags=["analysis"])

SUGGESTED_QUESTIONS = [
    "How much did I spend on groceries last month compared to the month before?",
    "Break down my spending by category for last month",
    "Show me my spending trend over the last 6 months",
    "What are my top 5 merchants this year?",
    "Which charges repeat every month?",
    "Show me all my Blue Bottle Coffee transactions",
    "How much did I spend in total last month?",
    "What is my average dining transaction this year?",
]


class AskRequest(BaseModel):
    question: str = Field(min_length=2, max_length=1000)
    use_cache: bool = True
    # A follow-up is (previous run, named refinement) — never free text, so it
    # carries no ambiguous conversational state.
    refine_from_run_id: uuid.UUID | None = None
    refinement: str | None = Field(default=None, max_length=120)


class RunSummary(BaseModel):
    id: uuid.UUID
    question: str
    status: str
    narration: str | None
    duration_ms: int
    created_at: datetime
    cached: bool


class CapabilitiesOut(BaseModel):
    ai_enabled: bool
    planner: str
    narrator: str
    disclosure: str
    suggested_questions: list[str]


def _sse(event: str, data: dict) -> str:
    """Encode one SSE frame. json.dumps never emits a raw newline, so the
    single-line `data:` form is safe."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


@router.get("/capabilities", response_model=CapabilitiesOut)
async def capabilities(user: CurrentUser) -> CapabilitiesOut:
    """What is actually powering answers right now — surfaced in the UI so the
    AI disclosure reflects reality rather than a marketing claim."""
    ai = settings.ai_available
    return CapabilitiesOut(
        ai_enabled=ai,
        planner="llm+rules" if ai else "rules",
        narrator="llm+template" if ai else "template",
        disclosure=(
            "Questions are interpreted by a deterministic rules engine and all "
            "figures are computed by SQL aggregates over your own data. No "
            "language model is configured, so none is called."
            if not ai
            else "A language model helps interpret questions and word explanations. "
            "All figures are still computed by SQL and verified before display."
        ),
        suggested_questions=SUGGESTED_QUESTIONS,
    )


@router.post("/runs")
async def ask(payload: AskRequest, request: Request, user: CurrentUser, session: DbSession):
    """Stream one analysis as Server-Sent Events."""
    await enforce(request, ANALYSIS_LIMIT, key=str(user.id))
    runner = AnalysisRunner(session, user.id, base_currency=user.base_currency)

    async def event_stream() -> AsyncIterator[str]:
        try:
            async for event in runner.run(
                payload.question,
                is_disconnected=request.is_disconnected,
                use_cache=payload.use_cache,
                refine_from=payload.refine_from_run_id,
                refinement=payload.refinement,
            ):
                yield _sse(event.event, event.data)
        except Exception:  # noqa: BLE001 - a stream must always terminate cleanly
            yield _sse(
                "error",
                {
                    "message": "The analysis stream failed unexpectedly. Please retry.",
                    "retryable": True,
                },
            )
        finally:
            yield _sse("done", {})

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # Stop nginx buffering the stream in production.
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/runs", response_model=list[RunSummary])
async def list_runs(user: CurrentUser, session: DbSession, limit: int = 20) -> list[RunSummary]:
    runs = (
        await session.execute(
            user_analysis_runs(user.id)
            .where(AnalysisRun.status == AnalysisStatus.COMPLETE)
            .order_by(desc(AnalysisRun.created_at))
            .limit(min(limit, 50))
        )
    ).scalars().all()
    return [
        RunSummary(
            id=run.id,
            question=run.question,
            status=run.status,
            narration=run.narration,
            duration_ms=run.duration_ms,
            created_at=run.created_at,
            cached=run.served_from_cache,
        )
        for run in runs
    ]


@router.get("/runs/{run_id}")
async def get_run(run_id: uuid.UUID, user: CurrentUser, session: DbSession) -> dict:
    """Re-open a past analysis with all of its inspectable steps."""
    run = (
        await session.execute(
            select(AnalysisRun)
            .options(selectinload(AnalysisRun.steps))
            .where(AnalysisRun.id == run_id, AnalysisRun.user_id == user.id)
        )
    ).scalar_one_or_none()

    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Analysis not found")

    return {
        "run_id": str(run.id),
        "question": run.question,
        "status": run.status,
        "plan": run.plan,
        "result": run.result,
        "chart": run.chart_spec,
        "narration": run.narration,
        "duration_ms": run.duration_ms,
        "steps": [
            {
                "seq": step.seq,
                "step": step.step,
                "status": step.status,
                "title": step.title,
                "payload": step.payload,
                "duration_ms": step.duration_ms,
            }
            for step in run.steps
        ],
    }
