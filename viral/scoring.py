"""Viral scoring: how a video performed, and how a new version might.

Two different questions, deliberately two different numbers
-----------------------------------------------------------
`viral_score` is a measurement. It reads what a video actually did -- views
against the account's following, engagement, how fast it accumulated -- and
grades it 0-100. Given the same inputs it always returns the same number.

`new_version_probability` is a forecast, and it is a weaker thing. It starts
from the original's measured score, because a format that worked once is
evidence, then adjusts for what is different about the new version and how
stale the trend is. It is expressed as a percentage because that is what the
brief asks for, and labelled an estimate everywhere it surfaces.

Missing data lowers confidence; it is never imputed
---------------------------------------------------
Follower counts and share counts are unavailable on most platforms. The
tempting move is to guess a median and carry on, which produces a confident
number built on invented inputs. Instead every signal records whether it was
actually observed, the score is computed from what is present, and confidence
falls as coverage drops. A score of 80 from two signals is reported as low
confidence, not as 80.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Confidence(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Potential(str, Enum):
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"
    EXCEPTIONAL = "exceptional"


@dataclass
class VideoMetrics:
    """What a platform actually told us. `None` means "not provided"."""

    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    followers: int | None = None
    posted_at: datetime | None = None
    duration_seconds: float | None = None
    platform: str = ""

    def age_hours(self, now: datetime | None = None) -> float | None:
        if self.posted_at is None:
            return None
        reference = now or datetime.now(timezone.utc)
        posted = self.posted_at
        if posted.tzinfo is None:
            posted = posted.replace(tzinfo=timezone.utc)
        hours = (reference - posted).total_seconds() / 3600.0
        return max(hours, 0.0)

    @property
    def engagements(self) -> int | None:
        parts = [p for p in (self.likes, self.comments, self.shares) if p is not None]
        return sum(parts) if parts else None


@dataclass
class Signal:
    """One graded input, with whether it was measured or absent."""

    name: str
    value: float          # 0..1 after normalisation
    weight: float
    observed: bool
    detail: str = ""


@dataclass
class ScoreBreakdown:
    """A score plus the arithmetic behind it, so it can be argued with."""

    score: int
    confidence: Confidence
    signals: list[Signal] = field(default_factory=list)
    coverage: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def observed_signals(self) -> list[Signal]:
        return [s for s in self.signals if s.observed]

    def to_record(self) -> dict:
        return {
            "score": self.score,
            "confidence": self.confidence.value,
            "coverage": round(self.coverage, 3),
            "signals": [
                {
                    "name": s.name,
                    "value": round(s.value, 4),
                    "weight": s.weight,
                    "observed": s.observed,
                    "detail": s.detail,
                }
                for s in self.signals
            ],
            "notes": list(self.notes),
            "is_estimate": True,
        }


def _log_ratio(value: float, midpoint: float) -> float:
    """Map a positive ratio onto 0..1, with `midpoint` landing at 0.5.

    Virality is multiplicative -- the step from 10k to 100k views means about
    as much as 100k to 1M -- so a log curve reflects it far better than a
    linear one, which would put every non-viral video at nearly zero.
    """
    if value <= 0:
        return 0.0
    scaled = math.log10(1.0 + value) / math.log10(1.0 + midpoint)
    return max(0.0, min(1.0, scaled / 2.0))


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


# Weights. Views-against-followers leads because it is the clearest evidence
# that a video escaped its own audience, which is what "viral" means; raw view
# count is weighted least because a large account gets it for free.
_WEIGHTS = {
    "views_per_follower": 0.30,
    "share_rate": 0.22,
    "engagement_rate": 0.20,
    "velocity": 0.18,
    "reach": 0.10,
}


def viral_score(
    metrics: VideoMetrics,
    *,
    now: datetime | None = None,
) -> ScoreBreakdown:
    """Grade observed performance 0-100 from whatever the platform provided."""
    signals: list[Signal] = []
    notes: list[str] = []
    views = metrics.views

    # 1. Views per follower: did it travel beyond the existing audience?
    if views is not None and metrics.followers:
        ratio = views / max(metrics.followers, 1)
        signals.append(Signal(
            "views_per_follower", _log_ratio(ratio, 10.0),
            _WEIGHTS["views_per_follower"], True,
            f"{ratio:.1f}x the account's followers",
        ))
    else:
        signals.append(Signal(
            "views_per_follower", 0.0, _WEIGHTS["views_per_follower"], False,
            "follower count not provided by this platform",
        ))
        notes.append(
            "Follower count unavailable, so the strongest breakout signal "
            "could not be measured."
        )

    # 2. Share rate: the clearest intent-to-spread signal there is.
    if views and metrics.shares is not None:
        rate = metrics.shares / max(views, 1)
        signals.append(Signal(
            "share_rate", _log_ratio(rate * 100, 1.0),
            _WEIGHTS["share_rate"], True, f"{rate * 100:.2f}% of viewers shared",
        ))
    else:
        signals.append(Signal(
            "share_rate", 0.0, _WEIGHTS["share_rate"], False,
            "share count not exposed by this platform",
        ))

    # 3. Engagement rate.
    engagements = metrics.engagements
    if views and engagements is not None:
        rate = engagements / max(views, 1)
        signals.append(Signal(
            "engagement_rate", _log_ratio(rate * 100, 6.0),
            _WEIGHTS["engagement_rate"], True,
            f"{rate * 100:.2f}% engaged",
        ))
    else:
        signals.append(Signal(
            "engagement_rate", 0.0, _WEIGHTS["engagement_rate"], False,
            "no engagement counts provided",
        ))

    # 4. Velocity: views per hour while the post is still young.
    age = metrics.age_hours(now)
    if views and age is not None and age >= 0.5:
        per_hour = views / age
        signals.append(Signal(
            "velocity", _log_ratio(per_hour, 5_000.0),
            _WEIGHTS["velocity"], True,
            f"{per_hour:,.0f} views/hour over {age:.0f}h",
        ))
    else:
        signals.append(Signal(
            "velocity", 0.0, _WEIGHTS["velocity"], False,
            "post age unavailable or too recent to measure",
        ))

    # 5. Raw reach, weighted least.
    if views is not None:
        signals.append(Signal(
            "reach", _log_ratio(views, 500_000.0), _WEIGHTS["reach"], True,
            f"{views:,} views",
        ))
    else:
        signals.append(Signal(
            "reach", 0.0, _WEIGHTS["reach"], False, "view count not provided",
        ))

    observed = [s for s in signals if s.observed]
    observed_weight = sum(s.weight for s in observed)
    coverage = observed_weight / sum(_WEIGHTS.values())

    if not observed:
        return ScoreBreakdown(
            0, Confidence.LOW, signals, 0.0,
            notes + ["No metrics were available, so no score could be computed."],
        )

    # Renormalise over what was measured, so absent signals do not silently
    # drag a good video toward zero.
    weighted = sum(s.value * s.weight for s in observed) / observed_weight
    score = int(round(_clamp01(weighted) * 100))

    if coverage >= 0.75:
        confidence = Confidence.HIGH
    elif coverage >= 0.45:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW
        notes.append(
            f"Only {coverage:.0%} of the scoring signals were available; treat "
            f"this score as indicative."
        )
    return ScoreBreakdown(score, confidence, signals, coverage, notes)


@dataclass
class NewVersionInputs:
    """What is known about the version the user is about to publish.

    Every field is optional. Each one present sharpens the estimate; each one
    absent widens it, rather than being assumed average.
    """

    hook_strength: float | None = None        # 0..1, from analysis
    retention_potential: float | None = None  # 0..1, from analysis
    topic_freshness: float | None = None      # 0..1, 1 = trend still rising
    length_fit: float | None = None           # 0..1, fit to platform norms
    platform: str = ""
    is_reused_format: bool = True             # a new take on a proven format


# How far a strong or weak new-version signal can move the forecast off the
# original's score, in points. Kept modest on purpose: the original's measured
# performance is evidence, while these are judgements about an unpublished
# video, and letting a judgement outweigh a measurement would be false
# precision dressed up as a percentage.
_ADJUSTMENT_RANGE = {
    "hook_strength": 12.0,
    "retention_potential": 10.0,
    "topic_freshness": 10.0,
    "length_fit": 6.0,
}


def new_version_probability(
    original: ScoreBreakdown,
    inputs: NewVersionInputs | None = None,
    *,
    now: datetime | None = None,
) -> ScoreBreakdown:
    """Estimate 0-100% that a new version performs well. Never a guarantee."""
    inputs = inputs or NewVersionInputs()
    notes: list[str] = [
        "This is an estimate from observed signals, not a guarantee of "
        "performance.",
    ]
    signals: list[Signal] = []

    base = float(original.score)
    adjustment = 0.0
    for name, span in _ADJUSTMENT_RANGE.items():
        raw = getattr(inputs, name, None)
        if raw is None:
            signals.append(Signal(name, 0.0, span, False, "not analysed"))
            continue
        value = _clamp01(float(raw))
        # 0.5 is neutral: below it subtracts, above it adds.
        adjustment += (value - 0.5) * 2.0 * span
        signals.append(Signal(name, value, span, True, f"{value:.2f}"))

    probability = base + adjustment

    # A fresh take on a proven format is the product's whole premise, but it
    # is still a different video: the ceiling stays below a straight repeat of
    # the original's own performance.
    if inputs.is_reused_format:
        probability = min(probability, base + 8.0)

    probability = int(round(max(1.0, min(95.0, probability))))
    if probability >= 95:
        notes.append("Capped at 95%: no short-form outcome is near-certain.")

    # Confidence is the weaker of the measurement's and the forecast's.
    analysed = [s for s in signals if s.observed]
    forecast_coverage = (
        sum(s.weight for s in analysed) / sum(_ADJUSTMENT_RANGE.values())
        if signals else 0.0
    )
    combined = min(original.coverage, forecast_coverage) if analysed else 0.0
    if combined >= 0.7 and original.confidence == Confidence.HIGH:
        confidence = Confidence.HIGH
    elif combined >= 0.4:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.LOW

    if not analysed:
        notes.append(
            "The new version has not been analysed, so this reflects only the "
            "original's performance."
        )
    return ScoreBreakdown(
        probability, confidence, signals, combined, notes + list(original.notes[:1])
    )


def potential_band(probability: int) -> Potential:
    """The headline label shown beside the percentage."""
    if probability >= 85:
        return Potential.EXCEPTIONAL
    if probability >= 65:
        return Potential.HIGH
    if probability >= 40:
        return Potential.MODERATE
    return Potential.LOW


POTENTIAL_DISPLAY = {
    Potential.EXCEPTIONAL: "🔥 Exceptional",
    Potential.HIGH: "🔥 High",
    Potential.MODERATE: "Moderate",
    Potential.LOW: "Low",
}


def summarise(
    original: ScoreBreakdown,
    forecast: ScoreBreakdown,
) -> dict:
    """The card payload: two numbers, a confidence, a band, and the caveat."""
    band = potential_band(forecast.score)
    return {
        "original_viral_score": original.score,
        "new_version_probability": forecast.score,
        "confidence": forecast.confidence.value,
        "potential": band.value,
        "potential_display": POTENTIAL_DISPLAY[band],
        "disclaimer": (
            "Estimated from observed signals. Not a guarantee of performance."
        ),
        "original_breakdown": original.to_record(),
        "forecast_breakdown": forecast.to_record(),
    }
