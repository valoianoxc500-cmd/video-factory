"""Stock footage, with a provider ladder and per-clip replacement.

Adapted from MoneyPrinterTurbo's `app/services/material.py` (MIT): the same two
free providers, the same "ask for landscape/portrait and pick the largest file
under the size cap" selection, the same content-hash dedup so one search does
not fill a video with the same clip.

The product rule this file exists to enforce is narrower than MPT's: **one
missing clip must never fail the video.** So every unit of work here is a
single beat, every failure is contained to that beat, and the caller gets back
whatever succeeded plus a list of what did not. A beat with no footage is
filled by a neighbour rather than raising.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
from dataclasses import dataclass
from pathlib import Path

import httpx

from settings import settings

logger = logging.getLogger("aivideo")

#: Smallest clip worth downloading. Anything under this is upscaled beyond
#: recognition on a 1080-wide canvas.
_MIN_WIDTH = 720
#: Biggest file worth downloading for one beat. Stock 4K originals run to
#: hundreds of MB and buy nothing after the vertical crop.
_MAX_BYTES = 60 * 1024 * 1024
_TIMEOUT = httpx.Timeout(45.0, connect=10.0)


@dataclass
class Clip:
    path: Path
    provider: str
    term: str
    width: int
    height: int
    duration: float
    source_url: str = ""
    #: (start, seconds) inside the source that sits within one shot. Set by
    #: `gather` from PySceneDetect so a beat does not open mid-edit; None
    #: means "start at zero", which is what the naive cut did.
    window: tuple[float, float] | None = None

    @property
    def is_portrait(self) -> bool:
        return self.height >= self.width


class NoFootage(RuntimeError):
    """Not one clip could be obtained from any provider."""


# ── providers ────────────────────────────────────────────────────────
#
# Each returns candidate descriptors, newest/largest first. They never raise
# for "nothing found" -- an empty list is a normal answer and the ladder moves
# on. They raise only on a transport failure the caller may want to retry.

async def _pexels_candidates(
    client: httpx.AsyncClient, term: str, portrait: bool
) -> list[dict]:
    key = (settings.pexels_api_key or "").strip()
    if not key or not key.isascii():
        return []
    resp = await client.get(
        "https://api.pexels.com/videos/search",
        headers={"Authorization": key},
        params={
            "query": term,
            "per_page": 12,
            "orientation": "portrait" if portrait else "landscape",
        },
    )
    if resp.status_code != 200:
        logger.debug(f"[aivideo] pexels {resp.status_code} for {term!r}")
        return []

    out: list[dict] = []
    for video in resp.json().get("videos", []):
        best = None
        for f in video.get("video_files", []):
            if (f.get("width") or 0) < _MIN_WIDTH:
                continue
            if best is None or (f.get("width") or 0) < (best.get("width") or 0):
                best = f          # smallest file that still clears the floor
        if best and best.get("link"):
            out.append({
                "url": best["link"],
                "width": best.get("width") or 0,
                "height": best.get("height") or 0,
                "duration": float(video.get("duration") or 0),
                "provider": "pexels",
                "page": video.get("url", ""),
            })
    return out


async def _pixabay_candidates(
    client: httpx.AsyncClient, term: str, portrait: bool
) -> list[dict]:
    key = (settings.pixabay_api_key or "").strip()
    if not key or not key.isascii():
        return []
    resp = await client.get(
        "https://pixabay.com/api/videos/",
        params={"key": key, "q": term, "per_page": 12, "video_type": "film"},
    )
    if resp.status_code != 200:
        logger.debug(f"[aivideo] pixabay {resp.status_code} for {term!r}")
        return []

    out: list[dict] = []
    for hit in resp.json().get("hits", []):
        streams = hit.get("videos") or {}
        for name in ("medium", "large", "small"):
            f = streams.get(name) or {}
            if not f.get("url") or (f.get("width") or 0) < _MIN_WIDTH:
                continue
            out.append({
                "url": f["url"],
                "width": f.get("width") or 0,
                "height": f.get("height") or 0,
                "duration": float(hit.get("duration") or 0),
                "provider": "pixabay",
                "page": hit.get("pageURL", ""),
            })
            break
    return out


#: Tried in order. Pexels first because its library is better curated for the
#: cinematic look this product wants; Pixabay is the fallback and needs no
#: header auth, so it also covers a Pexels outage.
PROVIDERS = (
    ("pexels", _pexels_candidates),
    ("pixabay", _pixabay_candidates),
)


def simplify(term: str) -> str:
    """Broaden a term that found nothing, rather than giving up on the beat.

    "abandoned desert highway" -> "desert highway" -> "desert". Each step drops
    the most specific word, which is the one most likely to be missing from a
    stock library.
    """
    words = term.split()
    return " ".join(words[1:]) if len(words) > 1 else ""


async def _download(
    client: httpx.AsyncClient, candidate: dict, target: Path, seen: set[str]
) -> Clip | None:
    try:
        async with client.stream("GET", candidate["url"]) as resp:
            if resp.status_code != 200:
                return None
            size = int(resp.headers.get("content-length") or 0)
            if size > _MAX_BYTES:
                logger.debug(f"[aivideo] skipping {size / 1e6:.0f}MB clip")
                return None
            body = bytearray()
            async for chunk in resp.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_BYTES:
                    return None
    except Exception as exc:
        logger.debug(f"[aivideo] download failed: {type(exc).__name__}: {exc}")
        return None

    if len(body) < 50_000:
        return None
    digest = hashlib.md5(bytes(body)).hexdigest()
    if digest in seen:
        return None                     # the same clip on two beats reads as a loop
    seen.add(digest)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(body))
    return Clip(
        path=target,
        provider=candidate["provider"],
        term=candidate.get("term", ""),
        width=candidate["width"],
        height=candidate["height"],
        duration=candidate["duration"],
        source_url=candidate.get("page", ""),
    )


async def fetch_one(
    term: str,
    target: Path,
    *,
    client: httpx.AsyncClient,
    seen: set[str],
    portrait: bool,
) -> Clip | None:
    """One beat's clip, walking providers then broadening the query.

    Returns None rather than raising: the caller decides what an uncovered
    beat means, and for this product it means "show a neighbouring clip
    longer", not "lose the video".
    """
    query = term
    while query:
        for name, search in PROVIDERS:
            try:
                candidates = await search(client, query, portrait)
            except Exception as exc:
                logger.info(
                    f"[aivideo] {name} unavailable for {query!r} "
                    f"({type(exc).__name__}); trying the next provider"
                )
                continue
            random.shuffle(candidates)          # avoid every video opening alike
            for candidate in candidates[:4]:
                candidate["term"] = term
                clip = await _download(client, candidate, target, seen)
                if clip:
                    logger.info(
                        f"[aivideo] {target.name}: {name} · {query!r} "
                        f"({clip.width}x{clip.height}, {clip.duration:.0f}s)"
                    )
                    return clip
        broader = simplify(query)
        if broader:
            logger.info(f"[aivideo] no footage for {query!r}; broadening to {broader!r}")
        query = broader
    return None


#: Candidates downloaded per beat before ranking. More is a better field to
#: choose from and more bandwidth; six is where the quality curve flattens on
#: the two free libraries.
CANDIDATES_PER_BEAT = 6


async def _collect_candidates(
    term_list: list[str],
    scratch: Path,
    *,
    client: httpx.AsyncClient,
    portrait: bool,
    wanted: int,
    providers=None,
) -> list[Clip]:
    """Download several distinct candidates for one beat.

    Walks every term and both providers until it has enough, so a beat whose
    first query is weak still gets a real field to choose from rather than one
    poor result. `seen` is per-beat here: two beats may legitimately consider
    the same clip, and the cross-beat duplicate rule is applied later where it
    can see what was actually chosen.
    """
    scratch.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    found: list[Clip] = []
    slot = 0
    provider_list = providers or PROVIDERS

    for term in term_list:
        query = term
        while query and len(found) < wanted:
            for name, search in provider_list:
                if len(found) >= wanted:
                    break
                try:
                    candidates = await search(client, query, portrait)
                except Exception as exc:
                    logger.info(
                        f"[aivideo] {name} unavailable for {query!r} "
                        f"({type(exc).__name__}); trying the next provider"
                    )
                    continue
                random.shuffle(candidates)
                for candidate in candidates:
                    if len(found) >= wanted:
                        break
                    candidate["term"] = term
                    clip = await _download(
                        client, candidate, scratch / f"cand_{slot:02d}.mp4", seen
                    )
                    slot += 1
                    if clip:
                        found.append(clip)
            if len(found) >= wanted:
                break
            query = simplify(query)
            if query:
                logger.debug(f"[aivideo] broadening {term!r} to {query!r}")

    return found


#: Canvas the motion pass renders a still onto. The clip is normalised again
#: downstream, so this only has to be at least the output size.
_STILL_SIZE = {True: (1080, 1920), False: (1920, 1080)}


async def _climb_ladder(
    chosen: list,
    beats: list,
    stranded: list[int],
    directory: Path,
    *,
    portrait: bool,
    intent_for,
    rank_pool,
    take,
    counts: dict,
    ledger=None,
) -> list:
    """Rungs 2-5 for the beats stock video could not cover.

    Returns `chosen` with whatever could be rescued filled in. Every rung is
    bounded and every failure is contained to its own beat: this runs after
    the video is already renderable, so nothing here may cost the render.
    """
    from aivideo import fallback
    from aivideo import rank as ranking

    alternatives = await fallback.propose_alternatives(
        [intent_for(beats[i]) for i in stranded], ledger=ledger
    )
    generated = 0

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        for position, index in enumerate(stranded):
            beat = beats[index]
            alternative = alternatives.get(position)
            scratch = directory / f"beat_{index:02d}" / "fallback"

            # ── rung 2: the same idea, filmed differently ──────────────
            if alternative and alternative.queries:
                counts["reconcepted"] += 1
                logger.info(
                    f"[aivideo] beat {index}: retrying as {alternative.concept[:70]!r}"
                )
                try:
                    pool = await _collect_candidates(
                        alternative.queries, scratch / "video",
                        client=client, portrait=portrait,
                        wanted=CANDIDATES_PER_BEAT,
                    )
                except Exception as exc:
                    logger.info(
                        f"[aivideo] beat {index}: related-concept search failed "
                        f"({type(exc).__name__})"
                    )
                    pool = []
                if pool:
                    order = await rank_pool(index, beat, pool, "_concept")
                    chosen[index] = take(index, pool, order)
                    if chosen[index] is not None:
                        counts["by_concept"] += 1
                        continue

            # ── rungs 3 and 4: a still, with motion on it ──────────────
            queries = list(alternative.still_queries) if alternative else []
            queries += [t for t in getattr(beat, "terms", []) if t not in queries]
            still = await _still_with_motion(
                client, index, beat, queries, scratch,
                portrait=portrait, intent_for=intent_for,
                ranking=ranking, ledger=ledger,
            )
            if still is not None:
                chosen[index] = take(index, [still], [0])
                if chosen[index] is not None:
                    counts["by_still"] += 1
                    continue

            # ── rung 5: one generated visual, generic concepts only ────
            if (
                alternative
                and not alternative.evidentiary
                and generated < fallback.MAX_GENERATED
            ):
                generated += 1
                image = await fallback.generated_still(
                    alternative.concept, scratch / "generated.png",
                    portrait=portrait, ledger=ledger,
                )
                if image is not None:
                    made = _motion_as_clip(
                        image, scratch / "generated.mp4", index,
                        portrait=portrait, term=alternative.concept[:60],
                        provider="generated",
                    )
                    if made is not None:
                        chosen[index] = take(index, [made], [0])
                        if chosen[index] is not None:
                            counts["by_generated"] += 1
                            logger.info(
                                f"[aivideo] beat {index}: covered by one "
                                f"generated visual (non-evidentiary concept)"
                            )
                            continue

            # ── rung 6: leave it; the caller holds a neighbour ─────────
            logger.info(
                f"[aivideo] beat {index}: no rung of the ladder covered "
                f"{intent_for(beat)[:60]!r}"
            )
    return chosen


async def _still_with_motion(
    client, index: int, beat, queries: list[str], scratch: Path, *,
    portrait: bool, intent_for, ranking, ledger,
):
    """A photograph that passes the ranker, turned into a moving shot."""
    from aivideo import fallback

    if not queries:
        return None
    try:
        stills = await fallback.still_candidates(
            client, queries[:3], scratch / "stills",
            portrait=portrait, wanted=4,
        )
    except Exception as exc:
        logger.info(f"[aivideo] beat {index}: still search failed ({type(exc).__name__})")
        return None
    if not stills:
        return None

    # Stills are ranked as they are: the ranker takes frames, and a photograph
    # is already a frame. A still that is not about the beat is no better than
    # a clip that is not about the beat.
    scores = await ranking.rank_candidates(
        [s["path"] for s in stills],
        intent=intent_for(beat), says=getattr(beat, "says", ""),
        ledger=ledger,
    )
    ranked = sorted(zip(stills, scores), key=lambda pair: -pair[1].score)
    for still, score in ranked:
        if not score.acceptable:
            break
        made = _motion_as_clip(
            still["path"], scratch / f"motion_{index:02d}.mp4", index,
            portrait=portrait, term=still.get("term", ""),
            provider=still.get("provider", "still"),
            source_url=still.get("page", ""),
        )
        if made is not None:
            logger.info(
                f"[aivideo] beat {index}: covered by a still with motion "
                f"({still.get('provider')} · {still.get('term')!r})"
            )
            return made
    return None


def _motion_as_clip(
    image: Path, target: Path, index: int, *, portrait: bool,
    term: str, provider: str, source_url: str = "",
) -> Clip | None:
    """Wrap the motion pass so a still enters the pipeline as an ordinary clip.

    Everything downstream -- fingerprinting, normalising, concatenation --
    then treats it exactly like footage, which is the whole point: a still
    with a slow push is a shot, not a special case.
    """
    from aivideo import fallback

    size = _STILL_SIZE[bool(portrait)]
    seconds = 5.0
    made = fallback.motion_clip(
        image, target, size=size, seconds=seconds, direction=index,
    )
    if made is None:
        return None
    return Clip(
        path=made, provider=provider, term=term,
        width=size[0], height=size[1], duration=seconds,
        source_url=source_url or f"still:{image.name}",
        window=(0.0, seconds),
    )


async def gather(
    beats: list,
    directory: Path,
    *,
    portrait: bool = True,
    concurrency: int = 3,
    rank: bool = True,
    ledger=None,
    reserves: list | None = None,
) -> tuple[list[Clip], list[str]]:
    """Fetch, rank and de-duplicate one clip per beat.

    Returns (chosen clips in beat order, beats that found nothing).

    The pipeline per beat is: collect several candidates, sample a frame from
    each, score those frames against the beat's visual intent, reject anything
    unrelated, then reject anything that looks like footage already chosen for
    another beat. That last step is what stops the same drone shot appearing
    three times, and it has to run across beats rather than within one, so
    selection is sequential even though downloading is not.

    Beats accept either the `Beat` objects the script stage now returns or
    plain search strings, so an older checkpoint still resumes.

    Pass `reserves` to have runner-up candidates banked into it -- extra
    distinct visuals, already ranked and de-duplicated, for the slots a video
    needs beyond one per beat.
    """
    from medialab import fingerprint, shots

    directory.mkdir(parents=True, exist_ok=True)
    limiter = asyncio.Semaphore(max(1, concurrency))

    def terms_for(beat) -> list[str]:
        raw = getattr(beat, "terms", None) or ([beat] if isinstance(beat, str) else [])
        return [t for t in raw if str(t).strip()] or [str(beat)]

    def intent_for(beat) -> str:
        return getattr(beat, "intent", None) or str(beat)

    pools: list[list[Clip]] = [[] for _ in beats]

    async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
        async def collect(index: int, beat) -> None:
            async with limiter:
                try:
                    pools[index] = await _collect_candidates(
                        terms_for(beat),
                        directory / f"beat_{index:02d}",
                        client=client,
                        portrait=portrait,
                        wanted=CANDIDATES_PER_BEAT,
                    )
                except Exception as exc:
                    logger.warning(
                        f"[aivideo] beat {index} candidate search failed "
                        f"({type(exc).__name__}: {exc}); continuing without it"
                    )

        await asyncio.gather(*(collect(i, b) for i, b in enumerate(beats)))

    # ── rank, then choose with the duplicate guard ───────────────────
    guard = fingerprint.DuplicateGuard()
    chosen: list[Clip | None] = [None] * len(beats)
    counts = {
        "unrelated": 0, "duplicate": 0, "retried": 0,
        "reconcepted": 0, "by_concept": 0, "by_still": 0, "by_generated": 0,
    }

    async def rank_pool(index: int, beat, pool: list[Clip], tag: str) -> list[int]:
        """Candidate positions worth trying, best first."""
        if not rank or len(pool) < 2:
            return list(range(len(pool)))

        from aivideo import rank as ranking

        frames_dir = directory / f"beat_{index:02d}" / f"frames{tag}"
        frames_dir.mkdir(parents=True, exist_ok=True)
        frames: list[Path] = []
        keep: list[int] = []
        for position, clip in enumerate(pool[: ranking.MAX_RANKED]):
            sampled = fingerprint.sample_frames(clip.path, count=1)
            if not sampled:
                continue
            target = frames_dir / f"f_{position:02d}.jpg"
            sampled[0].convert("RGB").save(target, quality=88)
            frames.append(target)
            keep.append(position)
        if not frames:
            return list(range(len(pool)))

        scores = await ranking.rank_candidates(
            frames, intent=intent_for(beat), says=getattr(beat, "says", ""),
            ledger=ledger,
        )
        scored = sorted(zip(keep, scores), key=lambda pair: -pair[1].score)
        counts["unrelated"] += sum(1 for _, s in scored if not s.acceptable)
        return [i for i, s in scored if s.acceptable]

    def take(index: int, pool: list[Clip], order: list[int]) -> Clip | None:
        """Best ranked candidate that is not a repeat of something chosen.

        Also banks one runner-up per beat as a reserve. A video always needs
        more on-screen slots than it has beats, and the alternative to a
        reserve is showing a beat's clip twice -- which is what made the
        second half of a video look like a rerun of the first. The runner-up
        is already downloaded, already ranked acceptable and already checked
        against the duplicate guard, so this costs nothing but the disk it is
        already using.
        """
        picked: Clip | None = None
        for position in order:
            clip = pool[position]
            if guard.seen_source(clip.source_url or str(clip.path.name)):
                counts["duplicate"] += 1
                continue
            signature = fingerprint.signature_for(clip.path)
            reason = guard.rejects(signature)
            if reason:
                counts["duplicate"] += 1
                logger.debug(f"[aivideo] beat {index}: skipped a clip {reason}")
                continue

            if clip.window is None:
                # Cut where the camera cuts, so the beat does not open
                # mid-edit. A clip that arrived with a window already set
                # chose it deliberately -- a still under a slow push has no
                # cuts to find and its whole length is the shot.
                clip.window = shots.longest_clean_window(clip.path, wanted=4.0)
            start, span = clip.window
            guard.accept(signature, clip.source_url)

            if picked is None:
                picked = clip
                logger.info(
                    f"[aivideo] beat {index}: {clip.provider} · {clip.term!r} "
                    f"({clip.width}x{clip.height}, window {start:.1f}s+{span:.1f}s)"
                )
                if reserves is None:
                    return picked
                continue          # look once more, for the reserve

            reserves.append(clip)
            logger.debug(f"[aivideo] beat {index}: banked a reserve visual")
            break
        return picked

    for index, (beat, pool) in enumerate(zip(beats, pools)):
        if not pool:
            continue
        order = await rank_pool(index, beat, pool, "")
        if not order:
            logger.info(
                f"[aivideo] beat {index}: every candidate was unrelated "
                f"to {intent_for(beat)[:60]!r}"
            )
        chosen[index] = take(index, pool, order)

    # ── one broadened second attempt for the beats that came up empty ─
    #
    # A beat whose whole field was rejected used to be abandoned there, and
    # the finished video simply held its neighbours longer. Before giving up
    # it is worth one wider query with the providers tried in the other
    # order: the narrow query is usually what failed, not the subject.
    #
    # Strictly one extra round per empty beat -- the point is a good pick,
    # not an unbounded search.
    stranded = [i for i, clip in enumerate(chosen) if clip is None]
    if stranded:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=True) as client:
            for index in stranded:
                beat = beats[index]
                wider = [q for q in (simplify(t) for t in terms_for(beat)) if q]
                if not wider:
                    continue
                counts["retried"] += 1
                try:
                    retry_pool = await _collect_candidates(
                        wider,
                        directory / f"beat_{index:02d}" / "retry",
                        client=client,
                        portrait=portrait,
                        wanted=CANDIDATES_PER_BEAT,
                        providers=list(reversed(PROVIDERS)),
                    )
                except Exception as exc:
                    logger.info(
                        f"[aivideo] beat {index}: second attempt failed "
                        f"({type(exc).__name__}); leaving it uncovered"
                    )
                    continue
                if not retry_pool:
                    continue
                order = await rank_pool(index, beat, retry_pool, "_retry")
                chosen[index] = take(index, retry_pool, order)
                if chosen[index] is not None:
                    logger.info(
                        f"[aivideo] beat {index}: covered on the second attempt "
                        f"with a broader query"
                    )

    # ── rungs 2-5: a related concept, a still, motion, one generated ──
    #
    # Anything still uncovered here has nothing in either video library under
    # any phrasing of its own wording. Leaving it uncovered means the finished
    # video pads over it with a neighbour, which is the pacing problem this
    # ladder exists to remove. Every rung still goes through the ranker.
    still_missing = [i for i, clip in enumerate(chosen) if clip is None]
    if still_missing:
        chosen = await _climb_ladder(
            chosen, beats, still_missing, directory,
            portrait=portrait, intent_for=intent_for,
            rank_pool=rank_pool, take=take, counts=counts, ledger=ledger,
        )

    clips = [c for c in chosen if c is not None]
    missing = [
        intent_for(b)[:60] for c, b in zip(chosen, beats) if c is None
    ]
    if not clips:
        raise NoFootage(
            "no stock provider returned usable footage for any search term"
        )

    logger.info(
        f"[aivideo] footage: {len(clips)}/{len(beats)} beats covered "
        f"({counts['unrelated']} rejected as unrelated, "
        f"{counts['duplicate']} as duplicates, "
        f"{counts['retried']} broadened, "
        f"{counts['by_concept']} by a related concept, "
        f"{counts['by_still']} by a still with motion, "
        f"{counts['by_generated']} generated)"
    )
    if missing:
        logger.info(
            f"[aivideo] {len(missing)} beat(s) found no usable footage; the "
            f"video will hold the surrounding clips longer"
        )
    return clips, missing
