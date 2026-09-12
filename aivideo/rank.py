"""Choosing which candidate clip actually shows what the beat is about.

Taking the first search result is why AI Video Maker produced unrelated
footage: a stock library's top hit for "gold vending machine" is frequently a
person holding a credit card. The fix is to fetch several candidates, look at
a frame from each, and pick on what the frame contains rather than on search
rank.

One batched vision call per beat, scoring every candidate at once. Bounded on
purpose -- one call, one image per candidate, a hard candidate cap -- because
the goal is a good pick, not the Football relevance apparatus. When the call
is unavailable the caller falls back to search order, which is where this
started, so a model outage costs quality rather than the video.

Why a hosted vision model rather than local CLIP embeddings: a local
CLIP-style ranker means torch (~2.5 GB) plus model weights on a machine with
13 GB free, for a signal this call already provides more accurately -- it can
read "is this the wrong sport / a stock watermark / a text-overlay thumbnail",
which cosine similarity cannot. The deterministic half of the problem, which
is duplicate and near-duplicate rejection, *is* local: see
medialab/fingerprint.py.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import clients

logger = logging.getLogger("aivideo")

__all__ = ["Scored", "rank_candidates", "MIN_ACCEPTABLE", "MAX_RANKED"]

#: Below this a candidate is rejected outright rather than used as a
#: best-of-a-bad-lot. A beat with no acceptable footage is better served by
#: the next provider, a broadened query, or a neighbouring clip held longer.
MIN_ACCEPTABLE = 5

#: Score at or above which the pick is confident enough to skip any further
#: work for this beat.
CONFIDENT = 8

#: Hard cap on images per ranking call. Eight frames is a big enough field to
#: choose from and keeps the call fast and cheap.
MAX_RANKED = 8


@dataclass
class Scored:
    index: int
    score: int
    reason: str = ""

    @property
    def acceptable(self) -> bool:
        return self.score >= MIN_ACCEPTABLE


def build_prompt(intent: str, says: str, count: int) -> str:
    return f"""Rank {count} candidate stock clips for one moment of a video.

WHAT THE VIEWER SHOULD SEE: {intent}
{f"WHAT THE NARRATOR SAYS HERE: {says}" if says else ""}

You are shown one frame from each candidate, numbered 1 to {count} in order.

Score each 0-10 for how well it illustrates the moment:
  9-10  exactly this subject and action
  7-8   clearly this subject, action close enough
  5-6   related and usable, but generic
  1-4   wrong subject, or so generic it illustrates nothing
  0     unusable

Score 0-2, whatever else is in frame:
- a different subject from the one described
- a stock-agency watermark, tiled logo or burned-in caption bar
- a screenshot, thumbnail with added text/arrows, or a slide
- an obvious AI-generated image with distorted hands, faces or text
- letterboxed, heavily blurred, or too dark to read
- a still photograph panned in software rather than real motion

Be strict about generic filler. A drone shot of an anonymous city skyline
scores 3 for almost any prompt: it is the single most overused stock shot and
it makes a video look automated. Only score it high if the moment is actually
about a skyline.

Return JSON only:
{{"scores": [{{"n": 1, "score": 7, "why": "six words"}}, ...]}}"""


async def rank_candidates(
    frames: list[Path],
    *,
    intent: str,
    says: str = "",
    operation_label: str = "aivideo_rank",
    ledger=None,
) -> list[Scored]:
    """Score each candidate frame against the beat.

    Returns one `Scored` per input frame, in input order. On any failure every
    candidate comes back at `MIN_ACCEPTABLE` so the caller falls back to
    search order rather than rejecting the whole beat.

    `ledger` records what the call actually cost. Ranking is now the largest
    line in a generation -- one vision call per beat, images included -- and
    leaving it out made the reported per-video cost the script call alone.
    """
    usable = [f for f in frames if f and f.exists()][:MAX_RANKED]
    if not usable:
        return []

    def account(model: str, input_tokens: int, output_tokens: int) -> None:
        if ledger is not None:
            ledger.record_model_call(
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                label="footage relevance",
            )

    neutral = [Scored(i, MIN_ACCEPTABLE, "not ranked") for i in range(len(usable))]
    try:
        answer = await clients.review_with_vision(
            build_prompt(intent, says, len(usable)),
            usable,
            operation_label=operation_label,
            on_usage=account,
        )
    except Exception as exc:
        logger.info(
            f"[aivideo] candidate ranking unavailable ({type(exc).__name__}); "
            f"falling back to search order"
        )
        return neutral

    if isinstance(answer, str):
        try:
            answer = json.loads(answer)
        except Exception:
            return neutral
    rows = (answer or {}).get("scores") if isinstance(answer, dict) else answer
    if not isinstance(rows, list):
        return neutral

    scored = list(neutral)
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("n", 0)) - 1
            score = max(0, min(10, int(row.get("score", 0))))
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(scored):
            scored[index] = Scored(index, score, str(row.get("why") or "")[:80])
    return scored
