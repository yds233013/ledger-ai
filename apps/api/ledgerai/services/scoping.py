"""User-isolation helpers.

Every user-owned read goes through one of these builders, so the ownership
predicate lives in a single place instead of being re-typed in every route
(where it can be forgotten). tests/test_isolation.py asserts that a second
user cannot reach the first user's rows through any API surface.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select

from ..models import Alert, AnalysisRun, ProcessingJob, Transaction, Upload


def user_transactions(user_id: uuid.UUID) -> Select[tuple[Transaction]]:
    return select(Transaction).where(Transaction.user_id == user_id)


def user_uploads(user_id: uuid.UUID) -> Select[tuple[Upload]]:
    return select(Upload).where(Upload.user_id == user_id)


def user_jobs(user_id: uuid.UUID) -> Select[tuple[ProcessingJob]]:
    return select(ProcessingJob).where(ProcessingJob.user_id == user_id)


def user_alerts(user_id: uuid.UUID) -> Select[tuple[Alert]]:
    return select(Alert).where(Alert.user_id == user_id)


def user_analysis_runs(user_id: uuid.UUID) -> Select[tuple[AnalysisRun]]:
    return select(AnalysisRun).where(AnalysisRun.user_id == user_id)
