"""What to show when the stock libraries have nothing for a beat.

Some beats are simply not in the free libraries. A real Arabic run asked for
"a 3D cutaway animation of a coral polyp secreting its skeleton" and "a
satellite view of the Great Barrier Reef", and neither exists on Pexels or
Pixabay at any usable quality. The old answer was to leave those beats
uncovered and let the surrounding clips cycle over them, which reads as
padding: the narration describes something specific while the picture shows
whatever came before.

So there is a ladder, tried in order, and every rung still has to pass the
same relevance bar as ordinary footage:

  1. a broader stock query                    (in footage.gather)
  2. a closely related *visual* concept that keeps the narration honest
  3. a high-quality still from the same libraries
  4. subtle motion on that still, so it reads as a deliberate shot
  5. one generated visual, and only for a generic, non-evidentiary concept
  6. holding a neighbour -- last resort, unchanged

Rung 2 is the interesting one. "A satellite view of the reef" and "an aerial
shot of the reef from a light aircraft" show the viewer the same idea; one is
findable and one is not. Rewriting to the findable neighbour keeps the beat
covered without making the narration wrong. What it must never do is drift to
something the narration does not support, which is why the rewrite is asked
for explicitly rather than inferred from keywords, and why the result goes
through the same ranker.

Rung 5 is deliberately narrow. Generating an image of a named place, a real
event or a real person would be inventing evidence, so the same call that
proposes the concept also says whether it is evidentiary, and only the
generic ones are eligible.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from settings import settings

logger = logging.getLogger("aivideo")

__all__ = [
    "Alternative", "propose_alternatives", "still_candidates",
    "motion_clip", "generated_still", "MAX_GENERATED",
]

#: At most one generated visual per video. The point is to rescue the odd
#: unfindable beat, not to become an image generator with a stock-footage
#: sideline.
MAX_GENERATED = 1

_MAX_STILL_BYTES = 12 * 1024 * 1024


@dataclass
class Alternative:
    """A findable way to show a beat the libraries could not cover."""

    #: What the viewer should see instead, still true to the narration.
    concept: str = ""
    #: Short English queries for that concept, footage first.
    queries: list[str] = field(default_factory=list)
    #: Queries for a still, which often exists where video does not.
    still_queries: list[str] = field(default_factory=list)
    #: True when the beat depicts something real and specific -- a named
    #: place, a real event, an identifiable person. Those may never be
    #: generated; a generic illustrative concept may.
    evidentiary: bool = True

    @property
    def usable(self) -> bool:
        return bool(self.concept and (self.queries or self.still_queries))


def build_prompt(intents: list[str]) -> str:
    listing = "\n".join(f"{i + 1}. {intent}" for i, intent in enumerate(intents))
    return f"""These shots could not be found in free stock video libraries.

For each one, give a CLOSELY RELATED visual that a stock library plausibly
does have, and that still honestly illustrates the same idea.

{listing}

Rules:
- The replacement must not change what the narration is saying. "A satellite
  view of a reef" may become "an aerial drone shot of a reef from above",
  because both show the reef's scale. It may NOT become "a beach", because
  that shows something else.
- Prefer a real, filmable subject over a diagram or an animation: stock
  libraries have almost no explanatory animation.
- "evidentiary" is true when the shot depicts something specific and real --
  a named place, a dated event, an identifiable person, a document. It is
  false for a generic illustrative subject ("a coral polyp close up",
  "water moving over sand").

Return JSON only:
{{"alternatives": [
  {{"n": 1,
    "concept": "one sentence, what the viewer should see",
    "queries": ["two word query", "another query"],
    "still_queries": ["query for a photograph"],
    "evidentiary": true}}
]}}"""


async def propose_alternatives(
    intents: list[str], *, ledger=None
) -> dict[int, Alternative]:
    """One batched call for every uncovered beat. Empty dict on any failure.

    Batched because this runs at most once per video and the beats are
    independent: one call for all of them costs a fraction of one call each,
    and an outage should cost the ladder a rung rather than the video.
    """
    if not intents:
        return {}

    import clients

    def account(model: str, input_tokens: int, output_tokens: int) -> None:
        if ledger is not None:
            ledger.record_model_call(
                model=model, input_tokens=input_tokens,
                output_tokens=output_tokens, label="visual fallback",
            )

    try:
        payload = await clients.generate_json(
            build_prompt(intents),
            system_instruction=(
                "You suggest findable stock-footage substitutes. You return "
                "JSON only, with no commentary before or after it."
            ),
            temperature=0.4,
            operation_label="aivideo_fallback_concepts",
        )
    except Exception as exc:
        logger.info(
            f"[aivideo] fallback concepts unavailable ({type(exc).__name__}); "
            f"the ladder will use the original wording"
        )
        return {}

    if ledger is not None:
        # `generate_json` reports no usage, so this is charged the same way the
        # script call is: derived from the text, and labelled as one line.
        rendered = json.dumps(payload, ensure_ascii=False) if payload else ""
        account(settings.gemini_primary_model,
                len(build_prompt(intents)) // 4, len(rendered) // 4)

    rows = (payload or {}).get("alternatives") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return {}

    out: dict[int, Alternative] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            index = int(row.get("n", 0)) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= index < len(intents):
            continue
        alternative = Alternative(
            concept=" ".join(str(row.get("concept") or "").split())[:280],
            queries=_clean(row.get("queries")),
            still_queries=_clean(row.get("still_queries")),
            # Default to evidentiary: generating is the risky branch, so an
            # absent or unreadable flag must not open it.
            evidentiary=bool(row.get("evidentiary", True)),
        )
        if alternative.usable:
            out[index] = alternative
    return out


def _clean(raw) -> list[str]:
    terms: list[str] = []
    for item in raw or []:
        term = " ".join(str(item).split()).lower()[:60]
        if term and term not in terms:
            terms.append(term)
    return terms[:3]


# ── rung 3: stills ───────────────────────────────────────────────────

async def still_candidates(
    client: httpx.AsyncClient, terms: list[str], scratch: Path, *,
    portrait: bool, wanted: int = 4, says: str = "",
) -> list[dict]:
    """Download a few candidate photographs. Each dict carries its local path.

    Routed through the media aggregator, so this reaches all twelve sources
    rather than the two stock libraries. That matters most exactly here: the
    beats that reach this rung are the ones stock video could not cover, and
    they are disproportionately the specific ones -- a named stadium, a dated
    event, a real satellite view -- which is what the archives have and stock
    does not.

    Only assets that passed the licence filter are returned, and each carries
    its licence and attribution onward.
    """
    from medialab.media import MediaType, SearchContext
    from medialab.media import search as media_search

    scratch.mkdir(parents=True, exist_ok=True)
    found: list[dict] = []
    seen: set[str] = set()
    slot = 0
    for term in terms:
        if len(found) >= wanted:
            break
        try:
            result = await media_search(
                term,
                media_type=MediaType.IMAGE,
                context=SearchContext(portrait=portrait, says=says, per_provider=4),
                client=client,
            )
        except Exception as exc:
            logger.info(f"[aivideo] media search failed ({type(exc).__name__})")
            continue
        for asset in result.assets:
            if len(found) >= wanted:
                break
            if not asset.download_url or asset.download_url in seen:
                continue
            seen.add(asset.download_url)
            target = scratch / f"still_{slot:02d}.jpg"
            slot += 1
            if await _download_still(client, asset.download_url, target):
                found.append({
                    "path": target, "term": term,
                    "provider": asset.provider,
                    "page": asset.original_url,
                    "width": asset.width, "height": asset.height,
                    "license": asset.license,
                    "license_url": asset.license_url,
                    "creator": asset.creator,
                    "attribution": asset.attribution,
                })
    return found


async def _download_still(client: httpx.AsyncClient, url: str, target: Path) -> bool:
    try:
        resp = await client.get(url)
        if resp.status_code != 200 or len(resp.content) > _MAX_STILL_BYTES:
            return False
        if len(resp.content) < 20_000:
            return False
        target.write_bytes(resp.content)
        return True
    except Exception as exc:
        logger.debug(f"[aivideo] still download failed: {type(exc).__name__}")
        return False


# ── rung 4: motion ───────────────────────────────────────────────────

#: Slow enough to read as a camera move rather than an effect. Measured
#: against the stock clips it sits between: anything faster draws attention to
#: the fact that it is a photograph.
_ZOOM_PER_FRAME = 0.0009
_ZOOM_LIMIT = 1.14


def motion_clip(
    image: Path, target: Path, *, size: tuple[int, int],
    seconds: float = 4.0, direction: int = 0,
) -> Path | None:
    """A still turned into a shot: a slow push, with a drift across the frame.

    `direction` alternates the drift so consecutive stills in one video do not
    move identically, which is what makes a Ken Burns pass look automated.
    """
    width, height = size
    frames = max(24, int(seconds * 30))
    # Oversample before the zoom: zoompan works on the scaled input, and
    # zooming a frame that is already at output size produces visible steps.
    big_w, big_h = width * 2, height * 2
    drift = {
        0: ("iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"),                 # centre
        1: (f"(iw-iw/zoom)*on/{frames}", "ih/2-(ih/zoom/2)"),        # left->right
        2: ("iw/2-(iw/zoom/2)", f"(ih-ih/zoom)*on/{frames}"),        # top->bottom
        3: (f"(iw-iw/zoom)*(1-on/{frames})", "ih/2-(ih/zoom/2)"),    # right->left
    }[direction % 4]

    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-loop", "1", "-t", f"{seconds:.2f}", "-i", str(image),
        "-vf",
        f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
        f"crop={big_w}:{big_h},"
        f"zoompan=z='min(zoom+{_ZOOM_PER_FRAME},{_ZOOM_LIMIT})':d={frames}:"
        f"x='{drift[0]}':y='{drift[1]}':s={width}x{height}:fps=30,"
        f"setsar=1,format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-an", str(target),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    except Exception as exc:
        logger.info(f"[aivideo] motion pass failed ({type(exc).__name__})")
        return None
    if result.returncode != 0 or not target.exists():
        logger.info(
            f"[aivideo] motion pass failed: "
            f"{(result.stderr or '').strip().splitlines()[-1:]}"
        )
        return None
    return target


# ── rung 5: one generated visual ─────────────────────────────────────

async def generated_still(
    concept: str, target: Path, *, portrait: bool, ledger=None
) -> Path | None:
    """One generated image for a generic concept. None when unavailable.

    The caller is responsible for the evidentiary check; this only refuses on
    configuration and failure.
    """
    import clients

    prompt = (
        f"A photorealistic documentary still: {concept}. "
        f"Natural lighting, real camera depth of field, no text, no watermark, "
        f"no logos, no people's faces in focus, no diagrams or labels."
    )
    try:
        produced = await clients.generate_image_gemini(
            prompt, target,
            aspect_ratio="9:16" if portrait else "16:9",
            operation_label="aivideo_fallback_image",
        )
    except Exception as exc:
        logger.info(
            f"[aivideo] generated visual unavailable ({type(exc).__name__}); "
            f"falling through to the last rung"
        )
        return None
    if produced is None or not Path(produced).exists():
        return None
    if ledger is not None:
        ledger.record_image(
            model=settings.gemini_image_model, label="generated visual"
        )
    return Path(produced)
