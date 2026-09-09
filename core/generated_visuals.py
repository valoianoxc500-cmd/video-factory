"""Last-resort generated visuals, for beats no real photograph could cover.

This exists because of a specific failure: a Horror Stories run that has done
everything right -- searched the web, run subject rescue, found real
photographs for most beats -- and is then abandoned wholesale because two or
three beats have nothing. Stopping the run was the honest answer while the only
alternative was passing generated frames off as documentary images. It is not
the only honest answer.

The rules that make this safe are not incidental, they are the feature:

  * Real photographs always win. Nothing here runs until web search and subject
    rescue have both failed for that specific slot.
  * A generated frame is never allowed to look like evidence. Prompts that name
    a real person, a real event, or ask for anything archival are refused --
    the refusal is the point, and the run fails as it did before rather than
    quietly producing a fake.
  * Every generated file is marked as generated in the provenance log, so no
    later stage can mistake one for a sourced photograph.
  * A hard per-video ceiling. A story needing more than a handful of invented
    frames is a story this pipeline should not be illustrating.

The review gates are untouched. A generated image still faces `image_review`
exactly as a sourced one does, and still fails the run if it is wrong.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

# Every other core module logs under "video_factory", and factory.py attaches
# the per-workspace file handler to that logger alone. A private logger name
# meant this module's refusals and costs never reached pipeline.html -- the
# fallback could fail completely and leave no trace in the run log.
logger = logging.getLogger("video_factory")

# Published per-image price for the fallback model. Recorded so the cost of
# rescuing a run is visible next to the cost of the run itself, rather than
# disappearing into the total.
#
# The fallback used to be one model, so one constant was the whole truth.
# Horror and True Stories now generate on FLUX Schnell at a different rate,
# and a single constant reported the same image at $0.0258 here and $0.009 in
# the cost tracker -- two prices for one picture, one of them in the run's own
# provenance. The rate is therefore looked up per model, and this constant
# remains the fallback for a model the catalog does not price.
COST_PER_IMAGE_USD = 0.00002 * 1290  # ~1290 output tokens per image


def cost_per_image(model: str) -> float:
    """The catalog's per-image rate for `model`, or the legacy estimate.

    Never raises and never blocks generation: an unpriced model still gets a
    number, and the cost tracker separately reports it as unpriced.
    """
    try:
        from core.costs import _find_price

        price = _find_price("generate_content_image", str(model or ""))
        if price is not None and price.output_image_rate_usd_per_image:
            return float(price.output_image_rate_usd_per_image)
    except Exception:  # pragma: no cover - pricing must never break sourcing
        pass
    return COST_PER_IMAGE_USD


class UnsafeVisualRequest(RuntimeError):
    """The requested visual cannot be generated without fabricating something."""


# --- what may never be generated -------------------------------------------

# Anything asking for a record of something that happened. A generated frame
# answering one of these is a forgery, whatever the caption says.
_ARCHIVAL_PATTERNS = (
    r"\barchival\b", r"\barchive\b", r"\bfootage\b", r"\bnewsreel\b",
    r"\bevidence\b", r"\bexhibit\b", r"\bcrime scene\b", r"\bautopsy\b",
    r"\bpolice (?:photo|report|file|sketch)\b", r"\bfbi\b", r"\bcia\b",
    r"\bcourt (?:record|document)\b", r"\bdeclassified\b", r"\bleaked\b",
    r"\bsurveillance (?:photo|still|camera|footage)\b", r"\bcctv\b",
    r"\bmugshot\b", r"\bdocumentary photograph\b", r"\bnews photo\b",
    r"\bhistorical (?:photo|photograph|record|document)\b",
    r"\bactual\b", r"\breal (?:photo|photograph|footage|image)\b",
    r"\bcomposite sketch\b", r"\bidentikit\b", r"\bwanted poster\b",
    r"\bnewspaper (?:clipping|front page)\b", r"\bpress photo\b",
    r"\bwitness (?:photo|statement)\b", r"\bofficial (?:photo|document)\b",
    # Named artefacts from real cases. Generating "the ransom note" produces a
    # prop that a documentary voiceover then describes as the real one.
    r"\bransom note\b", r"\bsuicide note\b", r"\bconfession\b",
    r"\bdeath certificate\b", r"\bflight recorder\b", r"\bblack box\b",
    r"\bdiary entry\b", r"\bmanifest\b", r"\bdental records\b",
)

# Portraiture of an identifiable person. A generated face presented as someone
# who exists is the single most damaging thing this module could produce.
_PERSON_PATTERNS = (
    r"\bportrait of\b", r"\bphoto of [A-Z]", r"\bface of\b",
    r"\blikeness\b", r"\bpolitician\b", r"\bpresident\b", r"\bcelebrity\b",
    r"\bvictim\b", r"\bsuspect\b", r"\bkiller\b", r"\bmurderer\b",
    r"\bthe missing (?:man|woman|girl|boy|child)\b",
)

_ARCHIVAL_RE = re.compile("|".join(_ARCHIVAL_PATTERNS), re.I)
_PERSON_RE = re.compile("|".join(_PERSON_PATTERNS), re.I)

# A capitalised multi-word name in the middle of a prompt is almost always a
# real person or a real named event. Sentence-initial capitals are excluded.
#
# The second alternative catches initial-style names -- D.B. Cooper, J. Edgar
# Hoover -- which the first misses entirely because "D.B." is not
# [A-Z][a-z]{2,}. That gap let "the D.B. Cooper ransom note" through, which is
# exactly the kind of subject this check exists to stop.
_PROPER_NOUN_RE = re.compile(
    r"\b[A-Z][a-z]{2,}\s+[A-Z][a-z]{2,}\b"
    r"|\b(?:[A-Z]\.\s*){1,3}\s*[A-Z][a-z]{2,}\b"
    r"|\b[A-Z][a-z]{2,}\s+(?:[A-Z]\.\s*){1,2}[A-Z][a-z]{2,}\b"
)


def refusal_reason(prompt: str, *, subject: str = "") -> str:
    """Why this visual must not be generated, or "" if it may be.

    Checked against the prompt and the slot's subject together: a clean prompt
    attached to "D.B. Cooper's ransom note" is still a request to fabricate an
    artefact from a real case.
    """
    text = f"{prompt} {subject}".strip()
    if not text:
        return "no prompt to generate from"

    match = _ARCHIVAL_RE.search(text)
    if match:
        return (
            f"asks for archival or evidentiary imagery ({match.group(0)!r}); "
            f"a generated frame would be a forgery"
        )
    match = _PERSON_RE.search(text)
    if match:
        return (
            f"asks for an identifiable person ({match.group(0)!r}); "
            f"generated likenesses of real people are not produced"
        )
    match = _PROPER_NOUN_RE.search(text)
    if match:
        return (
            f"names a specific person or event ({match.group(0)!r}); "
            f"only generic atmospheric imagery may be generated"
        )
    return ""


def is_safe_to_generate(prompt: str, *, subject: str = "") -> bool:
    return not refusal_reason(prompt, subject=subject)


# --- prompt construction ---------------------------------------------------

# Appended to every generated prompt. Says plainly what the frame is: mood, not
# record. The model is told to avoid text and faces because both are how a
# generated image starts looking like a document or a person.
_STYLE_SUFFIX = (
    "Cinematic atmospheric establishing shot, moody low-key lighting, "
    "shallow depth of field, film grain, vertical 9:16 composition. "
    "Generic and anonymous: no recognisable faces, no readable text, "
    "no logos, no documents, no newspaper or photographic artefacts. "
    "Evocative scene-setting imagery, not a photograph of any real event."
)


def build_prompt(brief: str, *, style_suffix: str = "") -> str:
    """Turn a slot brief into a prompt for an atmospheric, non-documentary frame."""
    base = " ".join(str(brief or "").split())
    parts = [base, style_suffix.strip(), _STYLE_SUFFIX]
    return " ".join(p for p in parts if p)


# --- accounting ------------------------------------------------------------

@dataclass
class GeneratedVisual:
    """One generated frame, and enough context to audit why it exists."""

    section_id: int
    sub_image_index: int
    file: str
    prompt: str
    model: str
    cost_usd: float

    def to_provenance(self) -> dict:
        """The provenance record. `generated: True` is the load-bearing field."""
        return {
            "section_id": self.section_id,
            "sub_image_index": self.sub_image_index,
            "file": self.file,
            # Read by downstream stages and by the review gates. A sourced
            # photograph never carries this.
            "generated": True,
            "source": "ai_generated_fallback",
            "provenance": "AI-generated illustration, not a photograph",
            "model": self.model,
            "prompt": self.prompt,
            "cost_usd": round(self.cost_usd, 6),
        }


@dataclass
class FallbackBudget:
    """The per-video ceiling, and what has been spent against it."""

    limit: int
    used: int = 0
    generated: list[GeneratedVisual] = field(default_factory=list)
    refused: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.remaining <= 0

    @property
    def total_cost_usd(self) -> float:
        return round(sum(v.cost_usd for v in self.generated), 6)

    def record(self, visual: GeneratedVisual) -> None:
        self.used += 1
        self.generated.append(visual)

    def refuse(self, reason: str) -> None:
        self.refused.append(reason)

    def summary(self) -> dict:
        return {
            "generated_images": self.used,
            "limit": self.limit,
            "refused": len(self.refused),
            "refusal_reasons": self.refused[:10],
            "total_cost_usd": self.total_cost_usd,
            # The rate actually charged for these frames, not a constant: a
            # run that generated on FLUX must not report the Gemini rate.
            "cost_per_image_usd": round(
                self.generated[0].cost_usd if self.generated else COST_PER_IMAGE_USD,
                6,
            ),
        }


def plan_fallback(
    missing: list[dict],
    *,
    limit: int,
    style_suffix: str = "",
) -> tuple[list[dict], FallbackBudget]:
    """Decide which missing slots may be generated, and with what prompt.

    Pure: no API calls, no files. Returns the work to do and a budget carrying
    the refusals, so a caller can log exactly why a slot was left empty.

    `missing` entries need `section_id`, `sub_image_index`, `brief`, and
    optionally `subject`.
    """
    budget = FallbackBudget(limit=max(0, int(limit)))
    planned: list[dict] = []

    for slot in missing:
        if budget.remaining <= len(planned):
            budget.refuse(
                f"section {slot.get('section_id')}: per-video limit of "
                f"{budget.limit} generated images reached"
            )
            continue

        brief = str(slot.get("brief") or "")
        subject = str(slot.get("subject") or "")
        reason = refusal_reason(brief, subject=subject)
        if reason:
            budget.refuse(f"section {slot.get('section_id')}: {reason}")
            logger.info(
                f"refusing to generate section {slot.get('section_id')} "
                f"sub-image {slot.get('sub_image_index')}: {reason}"
            )
            continue

        planned.append({
            "section_id": slot.get("section_id"),
            "sub_image_index": slot.get("sub_image_index"),
            "output_path": slot.get("output_path"),
            "prompt": build_prompt(brief, style_suffix=style_suffix),
        })

    return planned, budget


def record_generated(
    budget: FallbackBudget,
    *,
    section_id: int,
    sub_image_index: int,
    path: Path | str,
    prompt: str,
    model: str,
) -> GeneratedVisual:
    """Note a frame that was generated, with its cost."""
    visual = GeneratedVisual(
        section_id=int(section_id),
        sub_image_index=int(sub_image_index),
        file=Path(path).name,
        prompt=prompt,
        model=model,
        cost_usd=cost_per_image(model),
    )
    budget.record(visual)
    return visual
