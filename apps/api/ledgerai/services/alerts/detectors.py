"""Alert detectors.

Every detector is a pure function over a transaction and its history, so the
boundary cases — 7 samples versus 8, $24.99 versus $25.00, a MAD of zero — are
unit-testable without a database.

Two design points worth stating:

  * **Median and MAD, not mean and standard deviation.** The outlier being
    hunted inflates a standard deviation and thereby hides itself. The median
    absolute deviation does not move when one value is extreme.
  * **These are observations, not accusations.** Nothing here detects fraud.
    Every message describes a pattern in the user's own data, and every alert
    carries the disclaimer below.
"""

from __future__ import annotations

import statistics
import uuid
from dataclasses import dataclass, field
from datetime import date

from ...models import AlertSeverity, AlertType

ALERT_DISCLAIMER = (
    "Alerts describe unusual patterns in your own uploaded data. They are not "
    "fraud detection and do not mean anything is wrong."
)

# How severity maps to how the user should treat an alert. Duplicates are the
# only class that suggests an action; the rest are observations.
SEVERITY_INTENT: dict[AlertSeverity, str] = {
    AlertSeverity.HIGH: "Worth reviewing — you may have been charged twice.",
    AlertSeverity.MEDIUM: "Unusual compared with your own history. Not necessarily a problem.",
    AlertSeverity.LOW: "For information only.",
}

# --- thresholds, all named so a test can assert the boundary ---------------
NEAR_DUPLICATE_WINDOW_DAYS = 3
UNUSUAL_MIN_SAMPLES = 8
UNUSUAL_Z_THRESHOLD = 3.5
UNUSUAL_FLOOR_CENTS = 2_500          # $25.00
MAD_ZERO_MULTIPLE = 3.0              # fallback when every sample is identical
NEW_MERCHANT_FLOOR_CENTS = 5_000     # $50.00
LARGE_FOR_MERCHANT_MULTIPLE = 2.0
LARGE_FOR_MERCHANT_MIN_SAMPLES = 5
LARGE_FOR_MERCHANT_FLOOR_CENTS = 2_500
# A charge that is normal *for its own merchant* is not unusual, however it
# compares to the category. Without this, a $59.99 design subscription is
# flagged every month merely because most subscriptions cost $15.99, and every
# dinner is flagged because most dining charges are coffees.
MERCHANT_FAMILIAR_MIN_SAMPLES = 3
MERCHANT_FAMILIAR_MULTIPLE = 1.5

# Converts MAD to a standard-deviation-equivalent scale for normal data.
MAD_TO_SIGMA = 0.6745


@dataclass(slots=True, frozen=True)
class HistoricalCharge:
    """One prior transaction, reduced to what the detectors actually need."""

    transaction_id: uuid.UUID
    posted_date: date
    amount_cents: int      # negative for outflows, as stored
    merchant_key: str
    category_slug: str | None
    upload_id: uuid.UUID | None

    @property
    def magnitude(self) -> int:
        return abs(self.amount_cents)


@dataclass(slots=True)
class DetectionContext:
    """Everything a detector may look at for one candidate transaction."""

    transaction_id: uuid.UUID
    posted_date: date
    amount_cents: int
    merchant: str
    merchant_key: str
    category_slug: str | None
    category_name: str | None
    upload_id: uuid.UUID | None
    # Same category, trailing window, excluding this transaction.
    category_history: list[HistoricalCharge] = field(default_factory=list)
    # Same merchant, all time, excluding this transaction.
    merchant_history: list[HistoricalCharge] = field(default_factory=list)

    @property
    def magnitude(self) -> int:
        return abs(self.amount_cents)


@dataclass(slots=True)
class AlertCandidate:
    alert_type: AlertType
    severity: AlertSeverity
    message: str
    evidence: dict[str, object]


def robust_z_score(
    value: float, samples: list[float]
) -> tuple[float | None, dict[str, object]]:
    """Median/MAD-based z score, with the statistics that produced it.

    Returns (None, evidence) when there is not enough data to say anything —
    silence is the correct output for a small sample, not a guess.
    """
    if len(samples) < UNUSUAL_MIN_SAMPLES:
        return None, {"reason": "insufficient_history", "sample_size": len(samples)}

    median = statistics.median(samples)
    deviations = [abs(sample - median) for sample in samples]
    mad = statistics.median(deviations)

    evidence: dict[str, object] = {
        "sample_size": len(samples),
        "median_cents": round(median),
        "mad_cents": round(mad),
    }

    if mad == 0:
        # Every historical charge is identical, so a z score is undefined.
        # Fall back to a plain multiple of the median rather than dividing by
        # zero. The returned score is the caller's threshold, meaning "this
        # fired"; the real figure is reported as `multiple` in the evidence, so
        # nothing pretends a z score was computed.
        evidence["method"] = "median_multiple"
        evidence["multiple_threshold"] = MAD_ZERO_MULTIPLE
        if median > 0:
            evidence["multiple"] = round(value / median, 2)
            if value >= median * MAD_ZERO_MULTIPLE:
                return UNUSUAL_Z_THRESHOLD, evidence
        return None, evidence

    evidence["method"] = "median_absolute_deviation"
    score = MAD_TO_SIGMA * (value - median) / mad
    evidence["z_score"] = round(score, 2)
    return score, evidence


def detect_exact_duplicate(context: DetectionContext) -> AlertCandidate | None:
    """Same amount, merchant and date — arriving from a *different* upload.

    A statement can legitimately contain the same charge twice on one day (two
    identical coffees). Those share an upload and are not duplicates. Two
    uploads producing the same charge is the case worth flagging.
    """
    matches = [
        charge
        for charge in context.merchant_history
        if charge.posted_date == context.posted_date
        and charge.amount_cents == context.amount_cents
        and charge.upload_id != context.upload_id
    ]
    if not matches:
        return None

    return AlertCandidate(
        alert_type=AlertType.DUPLICATE,
        severity=AlertSeverity.HIGH,
        message=(
            f"A charge of {_money(context.magnitude)} at {context.merchant} on "
            f"{context.posted_date:%b %d} also appears in a different uploaded file. "
            "This may be the same purchase imported twice."
        ),
        evidence={
            "rule": "same amount, merchant and date, from a different upload",
            "match_count": len(matches),
            "matched_transaction_ids": [str(m.transaction_id) for m in matches[:5]],
            "amount_cents": context.amount_cents,
            "posted_date": context.posted_date.isoformat(),
            "disclaimer": ALERT_DISCLAIMER,
        },
    )


def detect_near_duplicate(context: DetectionContext) -> AlertCandidate | None:
    """Same amount and merchant within a few days."""
    matches = [
        charge
        for charge in context.merchant_history
        if charge.amount_cents == context.amount_cents
        and charge.posted_date != context.posted_date
        and abs((charge.posted_date - context.posted_date).days)
        <= NEAR_DUPLICATE_WINDOW_DAYS
    ]
    if not matches:
        return None

    nearest = min(
        matches, key=lambda c: abs((c.posted_date - context.posted_date).days)
    )
    gap = abs((nearest.posted_date - context.posted_date).days)

    return AlertCandidate(
        alert_type=AlertType.NEAR_DUPLICATE,
        # High alongside exact duplicates: both mean "you may have been charged
        # twice", which is the one thing here worth acting on promptly.
        severity=AlertSeverity.HIGH,
        message=(
            f"{context.merchant} charged {_money(context.magnitude)} twice within "
            f"{gap} day{'s' if gap != 1 else ''}. This can be a genuine repeat "
            "purchase or an accidental double charge."
        ),
        evidence={
            "rule": (
                f"same amount and merchant within {NEAR_DUPLICATE_WINDOW_DAYS} days"
            ),
            "match_count": len(matches),
            "nearest_transaction_id": str(nearest.transaction_id),
            "days_apart": gap,
            "amount_cents": context.amount_cents,
            "disclaimer": ALERT_DISCLAIMER,
        },
    )


def detect_unusual_amount(context: DetectionContext) -> AlertCandidate | None:
    """Unusually large for this category, by median and MAD."""
    if context.magnitude < UNUSUAL_FLOOR_CENTS:
        return None
    if context.category_slug is None:
        return None

    if _is_normal_for_merchant(context):
        return None

    samples = [float(charge.magnitude) for charge in context.category_history]
    score, evidence = robust_z_score(float(context.magnitude), samples)
    evidence.update(
        {
            "rule": (
                f"robust z score above {UNUSUAL_Z_THRESHOLD} using median and median "
                f"absolute deviation over the trailing window, with a "
                f"{_money(UNUSUAL_FLOOR_CENTS)} floor, and only when the amount is "
                f"also unusual for this merchant"
            ),
            "threshold": UNUSUAL_Z_THRESHOLD,
            "floor_cents": UNUSUAL_FLOOR_CENTS,
            "amount_cents": context.amount_cents,
            "category": context.category_name,
            "disclaimer": ALERT_DISCLAIMER,
        }
    )

    if score is None or score < UNUSUAL_Z_THRESHOLD:
        return None

    raw_median = evidence.get("median_cents", 0)
    median_cents = int(raw_median) if isinstance(raw_median, (int, float)) else 0
    return AlertCandidate(
        alert_type=AlertType.UNUSUAL_AMOUNT,
        severity=AlertSeverity.MEDIUM,
        message=(
            f"{_money(context.magnitude)} at {context.merchant} is much larger than "
            f"your usual {context.category_name} spending, where the typical charge "
            f"is about {_money(median_cents)}."
        ),
        evidence=evidence,
    )


def _is_normal_for_merchant(context: DetectionContext) -> bool:
    """True when this merchant routinely charges around this much.

    Compares against the merchant's own history before considering the category
    distribution, so a recurring subscription is never reported as an anomaly
    just for being pricier than its category's median.
    """
    if len(context.merchant_history) < MERCHANT_FAMILIAR_MIN_SAMPLES:
        return False
    merchant_median = statistics.median(
        charge.magnitude for charge in context.merchant_history
    )
    if merchant_median <= 0:
        return False
    return context.magnitude <= merchant_median * MERCHANT_FAMILIAR_MULTIPLE


def detect_new_merchant(context: DetectionContext) -> AlertCandidate | None:
    """First charge at a merchant, above a floor so small purchases stay quiet."""
    if context.merchant_history:
        return None
    if context.magnitude < NEW_MERCHANT_FLOOR_CENTS:
        return None

    return AlertCandidate(
        alert_type=AlertType.NEW_MERCHANT,
        severity=AlertSeverity.LOW,
        message=(
            f"First charge at {context.merchant}: {_money(context.magnitude)}. "
            "You have no earlier transactions with this merchant."
        ),
        evidence={
            "rule": (
                f"no prior transaction for this merchant and at least "
                f"{_money(NEW_MERCHANT_FLOOR_CENTS)}"
            ),
            "floor_cents": NEW_MERCHANT_FLOOR_CENTS,
            "amount_cents": context.amount_cents,
            "merchant": context.merchant,
            "disclaimer": ALERT_DISCLAIMER,
        },
    )


def detect_large_for_merchant(context: DetectionContext) -> AlertCandidate | None:
    """Much larger than this merchant has ever charged before."""
    if context.magnitude < LARGE_FOR_MERCHANT_FLOOR_CENTS:
        return None
    if len(context.merchant_history) < LARGE_FOR_MERCHANT_MIN_SAMPLES:
        return None

    previous_max = max(charge.magnitude for charge in context.merchant_history)
    if previous_max <= 0:
        return None
    if context.magnitude < previous_max * LARGE_FOR_MERCHANT_MULTIPLE:
        return None

    return AlertCandidate(
        alert_type=AlertType.LARGE_FOR_MERCHANT,
        severity=AlertSeverity.MEDIUM,
        message=(
            f"{_money(context.magnitude)} at {context.merchant} is more than "
            f"{LARGE_FOR_MERCHANT_MULTIPLE:g}× the largest amount you have previously "
            f"been charged there ({_money(previous_max)})."
        ),
        evidence={
            "rule": (
                f"at least {LARGE_FOR_MERCHANT_MULTIPLE:g}× the merchant's previous "
                f"maximum, with at least {LARGE_FOR_MERCHANT_MIN_SAMPLES} prior charges"
            ),
            "previous_max_cents": previous_max,
            "multiple": round(context.magnitude / previous_max, 2),
            "sample_size": len(context.merchant_history),
            "amount_cents": context.amount_cents,
            "disclaimer": ALERT_DISCLAIMER,
        },
    )


# Order matters: an exact duplicate should not also be reported as a near one.
DETECTORS = (
    detect_exact_duplicate,
    detect_near_duplicate,
    detect_unusual_amount,
    detect_new_merchant,
    detect_large_for_merchant,
)


def detect_all(context: DetectionContext) -> list[AlertCandidate]:
    """Run every detector, suppressing near-duplicate when exact already fired."""
    found: list[AlertCandidate] = []
    for detector in DETECTORS:
        candidate = detector(context)
        if candidate is None:
            continue
        if candidate.alert_type == AlertType.NEAR_DUPLICATE and any(
            existing.alert_type == AlertType.DUPLICATE for existing in found
        ):
            continue
        found.append(candidate)
    return found


def _money(cents: int) -> str:
    return f"${abs(cents) / 100:,.2f}"
