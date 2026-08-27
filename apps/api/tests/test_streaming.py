"""SSE cancellation and stream failure.

Phase 1 built these paths but never tested them. Both matter: an abandoned
analysis must stop doing work, and a failed one must still terminate the
stream cleanly rather than leaving the client hanging.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ledgerai.models import AnalysisRun, AnalysisStatus, User
from ledgerai.services.analysis import runner as runner_module
from ledgerai.services.analysis.runner import AnalysisRunner, StreamEvent

STEP_ORDER = ["understand", "select", "aggregate", "visualize", "explain"]


@pytest.fixture
def session(async_db: AsyncSession, demo_data: dict) -> AsyncSession:
    """The shared async session, with the seeded fixture already committed."""
    return async_db


@pytest.fixture
def user_id(demo_data: dict) -> uuid.UUID:
    return demo_data["user"].id


QUESTION = "Break down my spending by category for last month"


async def collect(runner: AnalysisRunner, **kwargs) -> list[StreamEvent]:
    return [event async for event in runner.run(QUESTION, **kwargs)]


class DisconnectAfter:
    """Reports a disconnect once `after` checks have happened."""

    def __init__(self, after: int) -> None:
        self.after = after
        self.checks = 0

    async def __call__(self) -> bool:
        self.checks += 1
        return self.checks > self.after


class TestCancellation:
    @pytest.mark.parametrize("after", [0, 1, 2, 3])
    async def test_cancelling_at_each_step_ends_cleanly(
        self, session: AsyncSession, user_id: uuid.UUID, after: int
    ) -> None:
        runner = AnalysisRunner(session, user_id)
        events = await collect(
            runner, use_cache=False, is_disconnected=DisconnectAfter(after)
        )

        assert events[-1].event == "cancelled"
        assert not any(event.event == "result" for event in events)
        assert not any(event.event == "error" for event in events)

    async def test_cancelled_run_is_recorded_as_cancelled(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        runner = AnalysisRunner(session, user_id)
        events = await collect(
            runner, use_cache=False, is_disconnected=DisconnectAfter(1)
        )
        run_id = uuid.UUID(events[-1].data["run_id"])

        run = (
            await session.execute(select(AnalysisRun).where(AnalysisRun.id == run_id))
        ).scalar_one()
        assert run.status == AnalysisStatus.CANCELLED

    async def test_partial_steps_are_kept_for_reference(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        """The user should still see how far the analysis got."""
        runner = AnalysisRunner(session, user_id)
        events = await collect(
            runner, use_cache=False, is_disconnected=DisconnectAfter(2)
        )
        completed = [
            event.data["step"]
            for event in events
            if event.event == "step" and event.data["status"] == "completed"
        ]
        assert completed
        assert completed[0] == "understand"

    async def test_cancelling_stops_before_finishing_the_work(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        early = await collect(
            AnalysisRunner(session, user_id), use_cache=False, is_disconnected=DisconnectAfter(0)
        )
        full = await collect(AnalysisRunner(session, user_id), use_cache=False)
        assert len(early) < len(full)


class TestStreamFailure:
    async def test_executor_failure_emits_a_retryable_error(
        self, session: AsyncSession, user_id: uuid.UUID, monkeypatch
    ) -> None:
        async def boom(*_args, **_kwargs):
            raise RuntimeError("database is on fire")

        monkeypatch.setattr(runner_module, "execute_plan", boom)

        events = await collect(AnalysisRunner(session, user_id), use_cache=False)
        error = events[-1]

        assert error.event == "error"
        assert error.data["retryable"] is True
        # The user is told something went wrong, not what the internals were.
        assert "on fire" not in error.data["message"]

    async def test_failed_run_is_recorded_as_failed(
        self, session: AsyncSession, user_id: uuid.UUID, monkeypatch
    ) -> None:
        async def boom(*_args, **_kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(runner_module, "execute_plan", boom)
        events = await collect(AnalysisRunner(session, user_id), use_cache=False)
        run_id = uuid.UUID(events[-1].data["run_id"])

        run = (
            await session.execute(select(AnalysisRun).where(AnalysisRun.id == run_id))
        ).scalar_one()
        assert run.status == AnalysisStatus.FAILED
        assert run.error_message is not None

    async def test_no_partial_answer_is_emitted_on_failure(
        self, session: AsyncSession, user_id: uuid.UUID, monkeypatch
    ) -> None:
        async def boom(*_args, **_kwargs):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(runner_module, "execute_plan", boom)
        events = await collect(AnalysisRunner(session, user_id), use_cache=False)
        assert not any(event.event == "result" for event in events)

    async def test_planner_failure_is_also_contained(
        self, session: AsyncSession, user_id: uuid.UUID, monkeypatch
    ) -> None:
        def boom(*_args, **_kwargs):
            raise ValueError("cannot plan")

        monkeypatch.setattr(runner_module.RulePlanner, "plan", boom)
        events = await collect(AnalysisRunner(session, user_id), use_cache=False)
        assert events[-1].event == "error"


class TestRefinementSafety:
    async def test_unknown_refinement_key_fails_closed(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        runner = AnalysisRunner(session, user_id)
        base = await collect(runner, use_cache=False)
        run_id = uuid.UUID(base[-1].data["run_id"])

        events = [
            event
            async for event in AnalysisRunner(session, user_id).run(
                "anything",
                use_cache=False,
                refine_from=run_id,
                refinement="drop_all_tables",
            )
        ]
        assert events[-1].event == "error"

    async def test_cannot_refine_another_users_run(
        self, session: AsyncSession, demo_data: dict
    ) -> None:
        owner = demo_data["user"].id
        other = demo_data["other"].id

        base = await collect(AnalysisRunner(session, owner), use_cache=False)
        run_id = uuid.UUID(base[-1].data["run_id"])

        events = [
            event
            async for event in AnalysisRunner(session, other).run(
                "anything",
                use_cache=False,
                refine_from=run_id,
                refinement="group_by_merchant",
            )
        ]
        assert events[-1].event == "error"

    async def test_refinement_produces_a_validated_plan(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        base = await collect(AnalysisRunner(session, user_id), use_cache=False)
        run_id = uuid.UUID(base[-1].data["run_id"])

        events = [
            event
            async for event in AnalysisRunner(session, user_id).run(
                "break down by merchant",
                use_cache=False,
                refine_from=run_id,
                refinement="group_by_merchant",
            )
        ]
        result = events[-1]
        assert result.event == "result"
        assert result.data["plan"]["group_by"] == "merchant"
        assert result.data["refined_from"]


class TestUserFacingSurface:
    async def test_every_step_is_streamed_in_order(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        events = await collect(AnalysisRunner(session, user_id), use_cache=False)
        completed = [
            event.data["step"]
            for event in events
            if event.event == "step" and event.data["status"] == "completed"
        ]
        assert completed == STEP_ORDER

    async def test_chart_matches_the_executed_result(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        """A chart that disagrees with the aggregation is worse than no chart."""
        events = await collect(AnalysisRunner(session, user_id), use_cache=False)
        payload = events[-1].data

        rows = payload["result"]["rows"]
        chart = payload["chart"]
        assert len(chart["data"]) == len(rows)
        for point, row in zip(chart["data"], rows, strict=True):
            assert point["value"] == pytest.approx(row["value"])

    async def test_supporting_transactions_belong_to_the_caller(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        events = await collect(AnalysisRunner(session, user_id), use_cache=False)
        supporting = events[-1].data["supporting_transactions"]
        assert supporting
        assert all("OTHER USER SECRET" not in row["description"] for row in supporting)

    async def test_today_is_injectable_so_results_are_deterministic(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        events = await collect(
            AnalysisRunner(session, user_id), use_cache=False, today=date(2026, 8, 26)
        )
        assert events[-1].event == "result"


class TestUserModel:
    async def test_base_currency_defaults_to_usd(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        user = (await session.execute(select(User).where(User.id == user_id))).scalar_one()
        assert user.base_currency == "USD"


class TestCachedReplayParity:
    """A cached answer must be indistinguishable from a live one."""

    async def test_cached_replay_still_offers_follow_ups(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        live = await collect(AnalysisRunner(session, user_id), use_cache=True)
        cached = await collect(AnalysisRunner(session, user_id), use_cache=True)

        assert cached[0].data["cached"] is True
        live_keys = {chip["key"] for chip in live[-1].data["refinements"]}
        cached_keys = {chip["key"] for chip in cached[-1].data["refinements"]}
        assert cached_keys == live_keys
        assert cached_keys

    async def test_cached_replay_keeps_the_same_steps_and_result(
        self, session: AsyncSession, user_id: uuid.UUID
    ) -> None:
        live = await collect(AnalysisRunner(session, user_id), use_cache=True)
        cached = await collect(AnalysisRunner(session, user_id), use_cache=True)

        live_steps = [e.data["step"] for e in live if e.event == "step"]
        cached_steps = [e.data["step"] for e in cached if e.event == "step"]
        assert cached_steps == live_steps
        assert cached[-1].data["result"] == live[-1].data["result"]
