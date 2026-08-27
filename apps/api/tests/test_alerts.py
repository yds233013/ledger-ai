"""Alert detectors: boundaries, false positives, and the not-fraud contract.

Every detector is a pure function, so each threshold is asserted exactly at its
edge rather than approximately.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest

from ledgerai.models import AlertSeverity, AlertType
from ledgerai.services.alerts.detectors import (
    ALERT_DISCLAIMER,
    LARGE_FOR_MERCHANT_MIN_SAMPLES,
    NEAR_DUPLICATE_WINDOW_DAYS,
    NEW_MERCHANT_FLOOR_CENTS,
    SEVERITY_INTENT,
    UNUSUAL_FLOOR_CENTS,
    UNUSUAL_MIN_SAMPLES,
    UNUSUAL_Z_THRESHOLD,
    DetectionContext,
    HistoricalCharge,
    detect_all,
    detect_exact_duplicate,
    detect_large_for_merchant,
    detect_near_duplicate,
    detect_new_merchant,
    detect_unusual_amount,
    robust_z_score,
)

UPLOAD_A = uuid.uuid4()
UPLOAD_B = uuid.uuid4()


def charge(
    cents: int,
    day: int = 1,
    month: int = 7,
    upload: uuid.UUID | None = None,
) -> HistoricalCharge:
    return HistoricalCharge(
        transaction_id=uuid.uuid4(),
        posted_date=date(2026, month, day),
        amount_cents=-abs(cents),
        merchant_key="sandbox grocers",
        category_slug="groceries",
        upload_id=upload or UPLOAD_A,
    )


def context(
    cents: int,
    *,
    day: int = 1,
    month: int = 8,
    category_history: list[HistoricalCharge] | None = None,
    merchant_history: list[HistoricalCharge] | None = None,
    upload: uuid.UUID | None = None,
    category_slug: str | None = "groceries",
) -> DetectionContext:
    return DetectionContext(
        transaction_id=uuid.uuid4(),
        posted_date=date(2026, month, day),
        amount_cents=-abs(cents),
        merchant="Sandbox Grocers",
        merchant_key="sandbox grocers",
        category_slug=category_slug,
        category_name="Groceries" if category_slug else None,
        upload_id=upload or UPLOAD_A,
        category_history=category_history or [],
        merchant_history=merchant_history or [],
    )


TYPICAL = [charge(c) for c in (4000, 4200, 3900, 4100, 4050, 3950, 4150, 4000)]


class TestRobustStatistics:
    def test_uses_median_and_mad_not_mean_and_stdev(self) -> None:
        """The point of MAD: one extreme value must not move the baseline."""
        samples = [100.0] * 8
        _, evidence = robust_z_score(500.0, [*samples, 100000.0])
        assert evidence["median_cents"] == 100
        assert evidence["mad_cents"] == 0

    def test_returns_nothing_below_the_sample_floor(self) -> None:
        score, evidence = robust_z_score(50000.0, [4000.0] * (UNUSUAL_MIN_SAMPLES - 1))
        assert score is None
        assert evidence["reason"] == "insufficient_history"

    def test_mad_of_zero_does_not_divide_by_zero(self) -> None:
        score, evidence = robust_z_score(3000.0, [1000.0] * 8)
        assert evidence["method"] == "median_multiple"
        assert score is not None


class TestUnusualAmount:
    def test_flags_a_genuine_outlier(self) -> None:
        alert = detect_unusual_amount(context(50_000, category_history=TYPICAL))
        assert alert is not None
        assert alert.alert_type == AlertType.UNUSUAL_AMOUNT
        assert alert.evidence["z_score"] > UNUSUAL_Z_THRESHOLD

    def test_ignores_a_typical_charge(self) -> None:
        assert detect_unusual_amount(context(4100, category_history=TYPICAL)) is None

    def test_requires_the_minimum_sample_size_exactly(self) -> None:
        too_few = TYPICAL[: UNUSUAL_MIN_SAMPLES - 1]
        assert detect_unusual_amount(context(50_000, category_history=too_few)) is None
        assert detect_unusual_amount(context(50_000, category_history=TYPICAL)) is not None

    def test_absolute_floor_boundary(self) -> None:
        tiny = [charge(1) for _ in range(8)]
        below = context(UNUSUAL_FLOOR_CENTS - 1, category_history=tiny)
        at = context(UNUSUAL_FLOOR_CENTS, category_history=tiny)
        assert detect_unusual_amount(below) is None
        assert detect_unusual_amount(at) is not None

    def test_uncategorized_transactions_are_not_compared(self) -> None:
        assert (
            detect_unusual_amount(
                context(50_000, category_history=TYPICAL, category_slug=None)
            )
            is None
        )

    def test_amount_normal_for_its_merchant_is_not_an_anomaly(self) -> None:
        """The false positive that motivated this rule: a $59.99 subscription
        flagged every month merely because most subscriptions cost $15.99."""
        subscription_history = [charge(5999, month=m) for m in (4, 5, 6)]
        cheap_category = [charge(1599) for _ in range(8)]
        alert = detect_unusual_amount(
            context(
                5999,
                category_history=cheap_category,
                merchant_history=subscription_history,
            )
        )
        assert alert is None

    def test_still_flags_a_merchant_charging_far_more_than_usual(self) -> None:
        coffee_history = [charge(800, month=m) for m in (4, 5, 6)]
        alert = detect_unusual_amount(
            context(
                18_400,
                category_history=[charge(c) for c in (700, 750, 800, 690, 720, 810, 760, 730)],
                merchant_history=coffee_history,
            )
        )
        assert alert is not None


class TestDuplicates:
    def test_same_day_repeat_within_one_upload_is_not_a_duplicate(self) -> None:
        """Two identical coffees on one day are two real purchases — which is
        exactly what source_row_index encodes at import time."""
        history = [charge(1850, day=1, month=8, upload=UPLOAD_A)]
        found = detect_exact_duplicate(
            context(1850, day=1, merchant_history=history, upload=UPLOAD_A)
        )
        assert found is None

    def test_same_charge_from_a_different_upload_is_a_duplicate(self) -> None:
        history = [charge(1850, day=1, month=8, upload=UPLOAD_A)]
        found = detect_exact_duplicate(
            context(1850, day=1, merchant_history=history, upload=UPLOAD_B)
        )
        assert found is not None
        assert found.severity == AlertSeverity.HIGH

    @pytest.mark.parametrize(
        ("gap_days", "expected"),
        [(1, True), (NEAR_DUPLICATE_WINDOW_DAYS, True), (NEAR_DUPLICATE_WINDOW_DAYS + 1, False)],
    )
    def test_near_duplicate_window_boundary(self, gap_days: int, expected: bool) -> None:
        history = [charge(8999, day=1 + gap_days, month=8, upload=UPLOAD_B)]
        found = detect_near_duplicate(context(8999, day=1, merchant_history=history))
        assert (found is not None) is expected

    def test_a_different_amount_is_not_a_near_duplicate(self) -> None:
        history = [charge(9000, day=2, month=8, upload=UPLOAD_B)]
        assert detect_near_duplicate(context(8999, day=1, merchant_history=history)) is None

    def test_exact_duplicate_suppresses_the_near_duplicate_report(self) -> None:
        history = [
            charge(1850, day=1, month=8, upload=UPLOAD_A),
            charge(1850, day=3, month=8, upload=UPLOAD_A),
        ]
        types = {
            found.alert_type
            for found in detect_all(
                context(1850, day=1, merchant_history=history, upload=UPLOAD_B)
            )
        }
        assert AlertType.DUPLICATE in types
        assert AlertType.NEAR_DUPLICATE not in types


class TestNewMerchant:
    def test_floor_boundary(self) -> None:
        assert detect_new_merchant(context(NEW_MERCHANT_FLOOR_CENTS - 1)) is None
        assert detect_new_merchant(context(NEW_MERCHANT_FLOOR_CENTS)) is not None

    def test_known_merchant_is_never_new(self) -> None:
        assert detect_new_merchant(context(50_000, merchant_history=[charge(100)])) is None

    def test_severity_is_low_because_a_new_merchant_is_not_alarming(self) -> None:
        found = detect_new_merchant(context(NEW_MERCHANT_FLOOR_CENTS))
        assert found is not None
        assert found.severity == AlertSeverity.LOW


class TestLargeForMerchant:
    def test_requires_enough_history(self) -> None:
        few = [charge(2000) for _ in range(LARGE_FOR_MERCHANT_MIN_SAMPLES - 1)]
        enough = [charge(2000) for _ in range(LARGE_FOR_MERCHANT_MIN_SAMPLES)]
        assert detect_large_for_merchant(context(9000, merchant_history=few)) is None
        assert detect_large_for_merchant(context(9000, merchant_history=enough)) is not None

    def test_just_under_twice_the_maximum_is_not_flagged(self) -> None:
        # Max is 2400, so the 2x boundary is 4800. Both probes sit above the
        # $25 absolute floor so only the multiple is under test.
        history = [charge(c) for c in (2000, 2200, 2400, 2100, 2300)]
        assert detect_large_for_merchant(context(4799, merchant_history=history)) is None
        assert detect_large_for_merchant(context(4800, merchant_history=history)) is not None

    def test_absolute_floor_applies_even_to_a_big_multiple(self) -> None:
        history = [charge(100) for _ in range(LARGE_FOR_MERCHANT_MIN_SAMPLES)]
        assert detect_large_for_merchant(context(2499, merchant_history=history)) is None
        assert detect_large_for_merchant(context(2500, merchant_history=history)) is not None


class TestSeverityPriority:
    """Severity encodes how the user should treat an alert.

    Duplicates are the only class that suggests an action; everything else is
    an observation about the user's own spending.
    """

    def test_both_duplicate_kinds_are_high_priority(self) -> None:
        exact = detect_exact_duplicate(
            context(
                1850,
                day=1,
                merchant_history=[charge(1850, day=1, month=8, upload=UPLOAD_A)],
                upload=UPLOAD_B,
            )
        )
        near = detect_near_duplicate(
            context(8999, day=1, merchant_history=[charge(8999, day=3, month=8, upload=UPLOAD_B)])
        )
        assert exact is not None and exact.severity == AlertSeverity.HIGH
        assert near is not None and near.severity == AlertSeverity.HIGH

    def test_unusual_amounts_are_medium_priority(self) -> None:
        found = detect_unusual_amount(context(50_000, category_history=TYPICAL))
        assert found is not None
        assert found.severity == AlertSeverity.MEDIUM

    def test_large_for_merchant_is_medium_priority(self) -> None:
        history = [charge(c) for c in (2000, 2200, 2400, 2100, 2300)]
        found = detect_large_for_merchant(context(9000, merchant_history=history))
        assert found is not None
        assert found.severity == AlertSeverity.MEDIUM

    def test_new_merchant_is_informational(self) -> None:
        found = detect_new_merchant(context(NEW_MERCHANT_FLOOR_CENTS))
        assert found is not None
        assert found.severity == AlertSeverity.LOW

    def test_every_severity_has_a_plain_language_meaning(self) -> None:
        for severity in (AlertSeverity.HIGH, AlertSeverity.MEDIUM, AlertSeverity.LOW):
            assert SEVERITY_INTENT[severity]

    def test_no_severity_note_asserts_wrongdoing(self) -> None:
        for note in SEVERITY_INTENT.values():
            lowered = note.lower()
            assert "fraud" not in lowered
            assert "unauthorized" not in lowered


class TestNotFraud:
    def test_every_alert_carries_the_disclaimer(self) -> None:
        found = detect_all(
            context(
                50_000,
                category_history=TYPICAL,
                merchant_history=[charge(1850, day=1, month=8, upload=UPLOAD_A)],
                upload=UPLOAD_B,
            )
        )
        assert found
        for alert in found:
            assert alert.evidence["disclaimer"] == ALERT_DISCLAIMER

    def test_no_message_claims_fraud_or_certainty(self) -> None:
        found = detect_all(context(50_000, category_history=TYPICAL))
        forbidden = ("fraud", "fraudulent", "unauthorized", "stolen", "scam")
        for alert in found:
            lowered = alert.message.lower()
            assert not any(word in lowered for word in forbidden)

    def test_evidence_explains_the_rule_that_fired(self) -> None:
        found = detect_unusual_amount(context(50_000, category_history=TYPICAL))
        assert found is not None
        assert "median" in found.evidence["rule"]
        assert found.evidence["sample_size"] == len(TYPICAL)
        assert found.evidence["threshold"] == UNUSUAL_Z_THRESHOLD
