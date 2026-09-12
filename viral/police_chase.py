"""Police Chase Studio policy and deterministic analysis helpers.

Pure functions live here so rights, captions, CTA and ranking can be tested
without a video, network request, model call or database.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

from viral.clipping import ViralMoment, select_viral_moments

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

def select_chase_moments(words: list, visual_events: list[dict], *, source_duration: float, target_seconds: int, count: int) -> list[ViralMoment]:
    length = "short" if target_seconds <= 30 else "medium" if target_seconds <= 60 else "long"
    base = select_viral_moments(words, source_duration=source_duration, length=length, count=max(count * 2, 3))
    candidates: list[ViralMoment] = []
    for moment in base:
        text = _words_in(words, moment.start, moment.end)
        hits = [label for label, terms in EVENT_TERMS.items() if any(term in text for term in terms)]
        boost = min(32, len(hits) * 12)
        signals = {**moment.signals, "supported_chase_events": boost}
        reason = hits[0].replace("_", " ").title() if hits else moment.reason
        candidates.append(ViralMoment("", moment.title, moment.start, moment.end, min(100, moment.score + boost), reason, signals))
    for event in visual_events or []:
        label = str(event.get("label") or "").lower()
        confidence = float(event.get("confidence") or 0)
        if label not in EVENT_LABELS or confidence < .65: continue
        at = max(0.0, min(float(source_duration), float(event.get("seconds") or 0)))
        start = max(0.0, at - target_seconds * .42)
        end = min(float(source_duration), start + target_seconds)
        start = max(0.0, end - target_seconds)
        score = min(100, 55 + round(confidence * 30))
        candidates.append(ViralMoment("", label.replace("_", " ").title(), round(start, 3), round(end, 3), score, label.replace("_", " ").title(), {"visual_event": round(confidence * 30)}))
    ranked = sorted(candidates, key=lambda m: (-m.score, m.start, m.end))
    selected: list[ViralMoment] = []
    for item in ranked:
        if any(max(0, min(item.end, x.end)-max(item.start, x.start))/max(.1, min(item.duration, x.duration)) > .35 for x in selected): continue
        selected.append(item)
        if len(selected) >= max(1, min(5, count)): break
    return [ViralMoment(f"chase_{i}", m.title, m.start, m.end, m.score, m.reason, m.signals) for i, m in enumerate(selected, 1)]
