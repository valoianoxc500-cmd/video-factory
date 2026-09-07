"""Analytics: what actually happened, against what was predicted.

A forecast nobody checks is decoration. This module pulls each published
post's real metrics from the platform that hosts it, scores them on the same
scale the prediction used, and reports the gap -- including when the gap says
the model is systematically optimistic.

Two honesty constraints shape the code:

* **Only the user's own posts.** These are insights endpoints for accounts the
  user connected; there is no path here that reads a stranger's analytics.
* **A small sample is said to be a small sample.** `calibration_report` refuses
  to present an accuracy figure as meaningful below `MIN_SAMPLE`, because five
  posts cannot tell you whether a predictor works.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from viral.scoring import (
    Confidence,
    ScoreBreakdown,
    VideoMetrics,
    potential_band,
    viral_score,
)

logger = logging.getLogger("viral.analytics")

#: Below this many published-and-measured posts, an accuracy number is noise.
MIN_SAMPLE = 10

#: How long after publishing a post's numbers are still climbing steeply.
SETTLING_HOURS = 48.0


@dataclass
class MetricSnapshot:
    """One reading of one post at one moment."""

    platform: str
    post_id: str
    captured_at: datetime
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    followers: int | None = None

    def as_metrics(self, *, posted_at: datetime | None = None,
                   duration_seconds: float | None = None) -> VideoMetrics:
        return VideoMetrics(
            views=self.views, likes=self.likes, comments=self.comments,
            shares=self.shares, followers=self.followers,
            posted_at=posted_at, duration_seconds=duration_seconds,
            platform=self.platform,
        )

    def to_record(self) -> dict:
        return {
            "platform": self.platform,
            "post_id": self.post_id,
            "captured_at": self.captured_at.isoformat(),
            "views": self.views, "likes": self.likes,
            "comments": self.comments, "shares": self.shares,
            "followers": self.followers,
        }


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------

def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def fetch_youtube(post_id: str, token: str, *, client, now=None) -> MetricSnapshot | None:
    try:
        response = client.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "statistics", "id": post_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        response.raise_for_status()
        items = response.json().get("items") or []
    except Exception as exc:
        logger.info(f"youtube analytics unavailable for {post_id}: {exc}")
        return None
    if not items:
        return None
    stats = items[0].get("statistics", {})
    return MetricSnapshot(
        platform="youtube", post_id=post_id, captured_at=_now(now),
        views=_int(stats.get("viewCount")), likes=_int(stats.get("likeCount")),
        comments=_int(stats.get("commentCount")),
        # The Data API exposes no share count; None, never a fabricated zero.
        shares=None,
    )


def fetch_instagram(post_id: str, token: str, *, client, now=None) -> MetricSnapshot | None:
    try:
        response = client.get(
            f"https://graph.facebook.com/v21.0/{post_id}/insights",
            params={"metric": "plays,likes,comments,shares",
                    "access_token": token},
        )
        response.raise_for_status()
        rows = response.json().get("data") or []
    except Exception as exc:
        logger.info(f"instagram insights unavailable for {post_id}: {exc}")
        return None
    values = {}
    for row in rows:
        series = row.get("values") or [{}]
        values[row.get("name")] = _int(series[0].get("value"))
    return MetricSnapshot(
        platform="instagram", post_id=post_id, captured_at=_now(now),
        views=values.get("plays"), likes=values.get("likes"),
        comments=values.get("comments"), shares=values.get("shares"),
    )


def fetch_facebook(post_id: str, token: str, *, client, now=None) -> MetricSnapshot | None:
    try:
        response = client.get(
            f"https://graph.facebook.com/v21.0/{post_id}",
            params={"fields": "video_insights.metric(total_video_views),"
                              "likes.summary(true),comments.summary(true)",
                    "access_token": token},
        )
        response.raise_for_status()
        body = response.json()
    except Exception as exc:
        logger.info(f"facebook insights unavailable for {post_id}: {exc}")
        return None
    views = None
    for row in (body.get("video_insights", {}).get("data") or []):
        if row.get("name") == "total_video_views":
            views = _int((row.get("values") or [{}])[0].get("value"))
    return MetricSnapshot(
        platform="facebook", post_id=post_id, captured_at=_now(now),
        views=views,
        likes=_int(body.get("likes", {}).get("summary", {}).get("total_count")),
        comments=_int(body.get("comments", {}).get("summary", {}).get("total_count")),
    )


FETCHERS = {
    "youtube": fetch_youtube,
    "instagram": fetch_instagram,
    "facebook": fetch_facebook,
}


def collect(
    platform: str,
    post_id: str,
    token: str,
    *,
    client,
    now: datetime | None = None,
) -> MetricSnapshot | None:
    """One reading, or None when the platform cannot report on this post."""
    fetcher = FETCHERS.get(str(platform or "").strip().lower())
    if fetcher is None or not post_id:
        return None
    return fetcher(post_id, token, client=client, now=now)


# ---------------------------------------------------------------------------
# Predicted versus actual
# ---------------------------------------------------------------------------

@dataclass
class Comparison:
    """One prediction, measured against what the post actually did."""

    video_id: str
    platform: str
    predicted: float
    actual: float | None = None
    posted_at: datetime | None = None
    settled: bool = False
    note: str = ""

    @property
    def error(self) -> float | None:
        if self.actual is None:
            return None
        return round(self.actual - self.predicted, 1)

    @property
    def band_matched(self) -> bool | None:
        if self.actual is None:
            return None
        return potential_band(self.actual) is potential_band(self.predicted)

    def to_record(self) -> dict:
        return {
            "video_id": self.video_id,
            "platform": self.platform,
            "predicted": self.predicted,
            "actual": self.actual,
            "error": self.error,
            "band_matched": self.band_matched,
            "settled": self.settled,
            "note": self.note,
        }


def compare(
    *,
    video_id: str,
    predicted: ScoreBreakdown | float,
    snapshot: MetricSnapshot | None,
    posted_at: datetime | None = None,
    followers: int | None = None,
    now: datetime | None = None,
) -> Comparison:
    """Score the real numbers on the same scale the prediction used."""
    reference = _now(now)
    predicted_score = (
        predicted.score if isinstance(predicted, ScoreBreakdown) else float(predicted)
    )
    platform = snapshot.platform if snapshot else ""

    if snapshot is None:
        return Comparison(
            video_id=video_id, platform=platform, predicted=predicted_score,
            posted_at=posted_at,
            note="No metrics yet. The platform has not reported on this post.",
        )

    metrics = snapshot.as_metrics(posted_at=posted_at)
    if followers is not None and metrics.followers is None:
        metrics.followers = followers
    actual = viral_score(metrics, now=reference)

    settled = bool(
        posted_at
        and reference >= posted_at + timedelta(hours=SETTLING_HOURS)
    )
    note = ""
    if not settled:
        note = (
            f"Measured less than {SETTLING_HOURS:.0f}h after posting; short-form "
            f"reach keeps climbing, so this will move."
        )
    elif actual.confidence is Confidence.LOW:
        note = "The platform reported few signals, so the actual score is rough."

    return Comparison(
        video_id=video_id, platform=snapshot.platform,
        predicted=predicted_score, actual=actual.score,
        posted_at=posted_at, settled=settled, note=note,
    )


def calibration_report(comparisons: list[Comparison]) -> dict:
    """Is the predictor any good? Said plainly, including when we cannot tell."""
    measured = [c for c in comparisons if c.actual is not None and c.settled]
    if not measured:
        return {
            "sample": 0,
            "reliable": False,
            "message": (
                "No settled results yet. Predictions can be compared once "
                f"posts are at least {SETTLING_HOURS:.0f}h old."
            ),
        }

    errors = [c.error for c in measured if c.error is not None]
    mean_error = round(sum(errors) / len(errors), 1)
    mean_absolute = round(sum(abs(e) for e in errors) / len(errors), 1)
    band_hits = sum(1 for c in measured if c.band_matched)

    reliable = len(measured) >= MIN_SAMPLE
    if not reliable:
        message = (
            f"Only {len(measured)} settled result"
            f"{'s' if len(measured) != 1 else ''}. That is too few to judge "
            f"the predictor; treat these figures as provisional."
        )
    elif mean_error <= -8:
        message = (
            f"Predictions are running about {abs(mean_error):.0f} points "
            f"optimistic across {len(measured)} posts."
        )
    elif mean_error >= 8:
        message = (
            f"Predictions are running about {mean_error:.0f} points "
            f"conservative across {len(measured)} posts."
        )
    else:
        message = f"Predictions track actual performance across {len(measured)} posts."

    return {
        "sample": len(measured),
        "reliable": reliable,
        "mean_error": mean_error,
        "mean_absolute_error": mean_absolute,
        "band_accuracy": round(band_hits / len(measured), 2),
        "message": message,
    }


def growth(snapshots: list[MetricSnapshot]) -> dict:
    """Views added between the first and last reading, and the rate."""
    readings = sorted(
        [s for s in snapshots if s.views is not None], key=lambda s: s.captured_at)
    if len(readings) < 2:
        return {"measurable": False,
                "reason": "At least two readings are needed to show growth."}
    first, last = readings[0], readings[-1]
    hours = (last.captured_at - first.captured_at).total_seconds() / 3600.0
    added = (last.views or 0) - (first.views or 0)
    return {
        "measurable": True,
        "views_added": added,
        "hours": round(hours, 1),
        "views_per_hour": round(added / hours, 1) if hours > 0 else None,
        "from": first.captured_at.isoformat(),
        "to": last.captured_at.isoformat(),
    }


def _int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
