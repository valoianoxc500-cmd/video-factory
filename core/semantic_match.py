"""Local semantic ranking, so a vision model is only paid when it is needed.

Why not CLIP
------------
The obvious answer is OpenCLIP, and it was the first thing considered. It
means torch (~2.5 GB), a model download, and a cold-start cost on every worker
-- for a job whose inputs are already *described in words*. Every provider
hands back a caption or tag list with its result: Pexels `alt`, Pixabay `tags`,
Unsplash `alt_description`, Commons `title`. Ranking a beat's words against
those captions is a text problem, and the repo already has a measured scorer
for it in `_pexels_relevance`.

So this generalises that scorer to any provider result and adds the piece that
was missing: a confidence judgement about whether the local answer is good
enough to act on alone.

What this does and does not change
----------------------------------
It decides whether to *pay a vision model to rank a shortlist*. That call is a
convenience -- it picks the best of several candidates that all already
survived sourcing. It is not the quality gate.

`image_review` still sees every image and still rejects what does not match.
Nothing here lowers a threshold; it removes a paid ranking step when the free
ranking is unambiguous, and defers to the model whenever it is not.

Measured: candidate selection was ~50% of a run's model spend.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("video_factory")

#: A local pick is trusted only when it is both strong and clearly ahead.
#: Strong alone is not enough: two candidates that both describe the beat well
#: are exactly the case a vision model is worth paying for, because the choice
#: between them is visual rather than lexical.
HIGH_CONFIDENCE = 0.75
CLEAR_MARGIN = 0.20

#: Below this the shortlist is probably all wrong, and the vision model is
#: worth paying to say so -- it can reject the whole set, which the local
#: scorer has no way to express.
TOO_WEAK_TO_JUDGE = 0.30

#: Words that carry no visual signal, so they neither help nor penalise a
#: caption that omits them.
_STOPWORDS = frozenset("""
a an the of in on at to from with and or for by as is are was were be been
this that these those it its his her their there here into onto over under
above showing shows show seen looking view shot image photo photograph picture
close up wide angle scene depicting depicts featuring featured very really
""".split())


def tokens_of(text: str) -> list[str]:
    """The content words of a beat's brief, in order."""
    words = re.findall(r"[a-z0-9]+", str(text or "").lower())
    return [w for w in words if len(w) > 2 and w not in _STOPWORDS]


def describe(item) -> str:
    """Everything a provider told us about a result, as one searchable string.

    Providers describe results in different fields, and the useful words are
    spread across them -- Pixabay puts them in `tags`, Unsplash in
    `alt_description`, Commons in the file title.
    """
    parts = [
        getattr(item, "title", "") or "",
        getattr(item, "query", "") or "",
        getattr(item, "attribution", "") or "",
        getattr(item, "source_page", "") or "",
    ]
    return re.sub(r"[^a-z0-9]+", " ", " ".join(parts).lower())


def relevance(text: str, tokens: list[str]) -> float:
    """0..1 rating of how well a description matches the beat's own words.

    Same model as the measured Pexels scorer: coverage carries most of the
    score, and adjacency carries the rest, because two query words side by
    side usually name one thing -- "fishing boat" -- where the same two
    scattered through a caption often name two.
    """
    if not tokens:
        return 0.0
    haystack = re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())
    matched = sum(1 for token in tokens if token in haystack)
    score = 0.85 * (matched / len(tokens))
    for first, second in zip(tokens, tokens[1:]):
        if f"{first} {second}" in haystack:
            score += 0.15
            break
    return min(1.0, score)


@dataclass
class LocalRanking:
    """The free answer, and whether it is worth paying to improve on."""

    scores: list[float]
    order: list[int]

    @property
    def best(self) -> float:
        return self.scores[self.order[0]] if self.order else 0.0

    @property
    def runner_up(self) -> float:
        return self.scores[self.order[1]] if len(self.order) > 1 else 0.0

    @property
    def margin(self) -> float:
        return self.best - self.runner_up

    @property
    def winner_index(self) -> int:
        """Index into the original candidate list, or -1 when there is none."""
        return self.order[0] if self.order else -1

    @property
    def confident(self) -> bool:
        """Whether the local pick can be trusted without a vision call."""
        return self.best >= HIGH_CONFIDENCE and self.margin >= CLEAR_MARGIN

    @property
    def too_weak(self) -> bool:
        """Whether the whole shortlist looks wrong, which only a model can
        properly confirm -- and rejecting the set is a verdict the local
        scorer cannot give."""
        return self.best < TOO_WEAK_TO_JUDGE

    def reason(self) -> str:
        if self.confident:
            return (f"local match {self.best:.2f} "
                    f"(+{self.margin:.2f} over the next)")
        if self.too_weak:
            return f"local match only {self.best:.2f}; asking the reviewer"
        return (f"local match {self.best:.2f} but only +{self.margin:.2f} "
                f"ahead; asking the reviewer")


def rank(descriptions: list[str], brief: str) -> LocalRanking:
    """Score and order candidate descriptions against a beat's brief."""
    tokens = tokens_of(brief)
    scores = [relevance(text, tokens) for text in descriptions]
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    return LocalRanking(scores=scores, order=order)


def rank_items(items: list, brief: str) -> LocalRanking:
    """Same, for provider results rather than bare strings."""
    return rank([describe(item) for item in items], brief)
