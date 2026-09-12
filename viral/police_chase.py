"""Police Chase Studio policy and deterministic analysis helpers.

Pure functions live here so rights, captions, CTA and ranking can be tested
without a video, network request, model call or database.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path

from medialab import fingerprint, shots as shotlib
from viral.clipping import ViralMoment, select_viral_moments

logger = logging.getLogger("viral.police_chase")

EVENT_TERMS = {
    "pursuit_start": ("pursuit", "chase began", "taking off", "fleeing"),
    "dangerous_maneuver": ("wrong way", "high speed", "swerving", "near miss"),
    "crash": ("crash", "collision", "hit the", "wreck"),
    "suspect_exit": ("get out", "exited", "ran from", "bailed out"),
    "foot_chase": ("foot pursuit", "running", "on foot"),
    "arrest": ("in custody", "arrest", "hands behind", "detained"),
    "spike_strip": ("spike strip", "stop sticks", "tire deflation"),
    "pit_maneuver": ("pit maneuver", "pit the vehicle", "precision immobilization"),
    "police_interaction": ("officer", "dispatch", "pull over", "traffic stop"),
    "resolution": ("ended", "came to a stop", "surrendered", "resolved"),
}
EVENT_LABELS = frozenset(EVENT_TERMS)

#: How much each event is worth. Previously every hit scored a flat 12, so a
#: dispatch radio call ranked level with a PIT manoeuvre and the clip that got
#: made was whichever happened to sit earlier in the transcript.
#:
#: The ordering is what a viewer actually stays for: the manoeuvre and the
#: impact first, the resolution next, and routine police interaction last --
#: it is context, not a moment.
EVENT_WEIGHTS = {
    "pit_maneuver": 34,
    "crash": 32,
    "dangerous_maneuver": 26,
    "spike_strip": 24,
    "suspect_exit": 20,
    "foot_chase": 18,
    "arrest": 18,
    "pursuit_start": 14,
    "resolution": 12,
    "police_interaction": 5,
}

#: Cuts per second above which a stretch of footage is "action". Dashcam and
#: bodycam rarely cut, so a burst of detected boundaries means the camera is
#: being thrown around -- which is what a crash or a hard manoeuvre looks like
#: to a content detector.
_ACTION_CUTS_PER_SECOND = 0.35
CTA_ALTERNATIVES = (
    "FOLLOW FOR MORE POLICE CHASES 🚔", "Follow for more police chases",
    "More pursuits coming", "Follow for daily police footage",
    "Follow for the next chase", "More police footage coming",
    "Follow for more wild pursuits",
)

@dataclass(frozen=True)
class SourceMetadata:
    title: str = ""
    source_agency: str = ""
    original_source_url: str = ""
    footage_date: str = ""
    location: str = ""
    reuse_basis: str = "owned_or_permitted"
    attribution_requirement: str = ""
    verification_status: str = "user_attested"
    notes: str = ""

    def to_record(self) -> dict: return asdict(self)

def _text(value: object, limit: int = 300) -> str:
    return " ".join(str(value or "").split())[:limit]

def normalize_source_metadata(raw: object) -> SourceMetadata:
    row = raw if isinstance(raw, dict) else {}
    basis = _text(row.get("reuse_basis"), 60).lower() or "owned_or_permitted"
    known = {"owned_or_permitted", "own_recording", "licensed", "permission", "creative_commons", "public_domain"}
    if basis not in known: basis = "owned_or_permitted"
    agency = _text(row.get("source_agency"), 120)
    url = _text(row.get("original_source_url"), 500)
    evidence = _text(row.get("attribution_requirement"), 300)
    verified = basis in {"public_domain", "creative_commons"} and bool(agency and url and evidence)
    return SourceMetadata(
        title=_text(row.get("title"), 180), source_agency=agency,
        original_source_url=url, footage_date=_text(row.get("footage_date"), 40),
        location=_text(row.get("location"), 120), reuse_basis=basis,
        attribution_requirement=evidence,
        verification_status="verified_reusable" if verified else "user_attested" if basis in {"owned_or_permitted", "own_recording", "licensed", "permission"} else "unclear",
        notes=_text(row.get("notes"), 500),
    )

def caption_decision(choice: str, burned_english: bool | None) -> dict:
    choice = choice if choice in {"auto", "en", "ar", "none"} else "auto"
    if choice == "none": return {"action": "none", "status": "Captions off"}
    if choice == "ar": return {"action": "translate_ar", "status": "Arabic translation added"}
    if burned_english is True: return {"action": "preserve", "status": "Original English captions detected"}
    if burned_english is None and choice == "auto": return {"action": "preserve", "status": "Original captions preserved"}
    return {"action": "generate_en", "status": "English captions generated"}

def choose_cta(mode: str, custom: str, seed: str, placement: str = "end") -> dict:
    placement = placement if placement in {"end", "persistent"} else "end"
    if mode == "off": return {"text": "", "placement": placement}
    if mode == "custom": return {"text": _text(custom, 90), "placement": placement}
    digest = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    return {"text": CTA_ALTERNATIVES[digest % len(CTA_ALTERNATIVES)], "placement": placement}

def _words_in(words: list, start: float, end: float) -> str:
    return " ".join(str(w.get("word") or "") for w in words if isinstance(w, dict) and start <= float(w.get("start") or -1) <= end).casefold()

def _event_boost(hits: list[str]) -> int:
    """What the events found in one stretch of transcript are worth together.

    Weighted and diminishing. Two strong events beat one, but a stretch that
    says "officer" and "dispatch" and "pull over" must not out-rank a single
    crash -- which is exactly what the old flat 12-per-hit did.
    """
    ordered = sorted((EVENT_WEIGHTS.get(hit, 8) for hit in hits), reverse=True)
    total = sum(weight / (index + 1) for index, weight in enumerate(ordered))
    return int(min(38, round(total)))


def _severity_of(hits: list[str]) -> str:
    """The event a viewer would name the clip after: the heaviest one."""
    if not hits: return ""
    return max(hits, key=lambda hit: EVENT_WEIGHTS.get(hit, 8))


def _align(start: float, end: float, shots: list, source_duration: float) -> tuple[float, float]:
    """Move a window onto shot boundaries when the footage has any.

    Continuous dashcam has none and this is a no-op, which is the honest
    answer -- there is no cut to align to.
    """
    if not shots: return round(start, 3), round(end, 3)
    wanted = max(1.0, end - start)
    at, span = shotlib.best_window_near((start + end) / 2, wanted=wanted, shots=shots, total=source_duration)
    return round(at, 3), round(min(source_duration, at + span), 3)


def action_candidates(shots: list, *, source_duration: float, target_seconds: int, limit: int = 6) -> list[ViralMoment]:
    """Moments proposed from the picture alone, where the camera is working hardest.

    Police sources are frequently one long unedited take with no transcript
    worth ranking, in which case the transcript path returns nothing usable
    and the product falls back to "first 30 seconds". A burst of detected
    content changes is the visual signature of a hard manoeuvre or an impact.

    These deliberately carry no event label. The detector sees motion, not a
    PIT manoeuvre, and naming one would be inventing an event.
    """
    if not shots or source_duration <= 0: return []
    boundaries = sorted({round(shot.start, 3) for shot in shots[1:]})
    if not boundaries: return []
    window = max(4.0, float(target_seconds))
    step = max(1.0, window / 2)
    found: list[ViralMoment] = []
    at = 0.0
    while at < max(0.0, source_duration - window * .5):
        end = min(source_duration, at + window)
        cuts = sum(1 for boundary in boundaries if at <= boundary <= end)
        if cuts / max(1.0, end - at) >= _ACTION_CUTS_PER_SECOND:
            found.append(ViralMoment(
                "", "High-motion sequence", round(at, 3), round(end, 3),
                min(100, 40 + min(28, cuts * 6)), "Sustained on-camera action",
                {"action_density": min(28, cuts * 6)},
            ))
        at += step
    found.sort(key=lambda m: (-m.score, m.start))
    return found[:limit]


def select_chase_moments(
    words: list,
    visual_events: list[dict],
    *,
    source_duration: float,
    target_seconds: int,
    count: int,
    video: Path | None = None,
) -> list[ViralMoment]:
    """Rank and pick the strongest moments in one authorized police source.

    Three kinds of evidence, none of them invented: what the transcript
    actually says, what a bounded vision pass actually saw, and where the
    picture itself is moving hardest. Selection then refuses repeats twice
    over -- once on time overlap, once on what the frames look like, because
    two windows seconds apart on the same impact do not overlap enough to be
    caught by the first test and are the same clip to a viewer.

    `video`, when given, unlocks the visual half: shot-aligned cuts,
    action-density candidates, and near-identical rejection. Without it the
    behaviour is the transcript-and-events ranking, unchanged.
    """
    length = "short" if target_seconds <= 30 else "medium" if target_seconds <= 60 else "long"
    base = select_viral_moments(words, source_duration=source_duration, length=length, count=max(count * 2, 3))
    detected = shotlib.detect_shots(video) if video is not None else []
    candidates: list[ViralMoment] = []
    for moment in base:
        text = _words_in(words, moment.start, moment.end)
        hits = [label for label, terms in EVENT_TERMS.items() if any(term in text for term in terms)]
        boost = _event_boost(hits)
        signals = {**moment.signals, "supported_chase_events": boost}
        strongest = _severity_of(hits)
        reason = strongest.replace("_", " ").title() if strongest else moment.reason
        start, end = _align(moment.start, moment.end, detected, source_duration)
        candidates.append(ViralMoment("", moment.title, start, end, min(100, moment.score + boost), reason, signals))
    for event in visual_events or []:
        label = str(event.get("label") or "").lower()
        confidence = float(event.get("confidence") or 0)
        if label not in EVENT_LABELS or confidence < .65: continue
        at = max(0.0, min(float(source_duration), float(event.get("seconds") or 0)))
        start = max(0.0, at - target_seconds * .42)
        end = min(float(source_duration), start + target_seconds)
        start = max(0.0, end - target_seconds)
        start, end = _align(start, end, detected, source_duration)
        weight = EVENT_WEIGHTS.get(label, 8)
        score = min(100, 44 + weight + round(confidence * 20))
        candidates.append(ViralMoment(
            "", label.replace("_", " ").title(), start, end, score,
            label.replace("_", " ").title(),
            {"visual_event": round(confidence * 20), "event_severity": weight},
        ))
    candidates.extend(action_candidates(detected, source_duration=source_duration, target_seconds=target_seconds))
    ranked = sorted(candidates, key=lambda m: (-m.score, m.start, m.end))
    wanted = max(1, min(5, count))
    selected: list[ViralMoment] = []
    guard = fingerprint.DuplicateGuard() if video is not None else None
    for item in ranked:
        if any(max(0, min(item.end, x.end)-max(item.start, x.start))/max(.1, min(item.duration, x.duration)) > .35 for x in selected): continue
        if guard is not None:
            signature = fingerprint.signature_between(video, item.start, item.end)
            if signature.usable:
                if guard.rejects(signature):
                    logger.info(f"[police-chase] skipped {item.start:.1f}s: shows the same thing as a moment already chosen")
                    continue
                guard.accept(signature)
        selected.append(item)
        if len(selected) >= wanted: break
    return [ViralMoment(f"chase_{i}", m.title, m.start, m.end, m.score, m.reason, m.signals) for i, m in enumerate(selected, 1)]
