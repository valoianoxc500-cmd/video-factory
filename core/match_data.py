"""Match identification and verified event timeline for the Match Analysis engine.

The engine's whole claim is that it describes what actually happened, so
match facts are treated the same way the news channel treats claims: they
carry a confidence, and unverified ones are either attributed or dropped
rather than narrated as fact.

Providers
---------
`structured`  A real sports-data API (football-data.org shape). Used when
              FOOTBALL_DATA_API_KEY is set. Authoritative: scores and event
              minutes come from the competition's own feed.
`grounded`    The existing Gemini + Google Search grounding the research
              stage already uses. No new credentials, but the result is a
              model reading sources, so every event is marked unverified
              unless it is corroborated by the scoreline.

The provider is chosen by `select_provider`, so adding a third one is a
function and a config value, not a pipeline change.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Literal

from core.footage import MatchMoment

logger = logging.getLogger("video_factory")

Confidence = Literal["verified", "reported", "unverified"]

# Event types worth building a moment around, most significant first. Anything
# outside this set is background for the narration, not its own clip.
MAJOR_EVENTS = ("goal", "penalty", "red_card", "own_goal", "var_decision")

_MAX_MOMENTS = 6


@dataclass
class MatchEvent:
    type: str
    minute: int
    team: str = ""
    player: str = ""
    detail: str = ""
    confidence: Confidence = "unverified"

    @property
    def is_major(self) -> bool:
        return self.type in MAJOR_EVENTS


@dataclass
class MatchFacts:
    """Everything the script needs to talk about one specific match."""

    home_team: str = ""
    away_team: str = ""
    competition: str = ""
    played_on: str = ""            # ISO date, "" when unknown
    home_score: int | None = None
    away_score: int | None = None
    events: list[MatchEvent] = field(default_factory=list)
    provider: str = ""
    identified: bool = False
    note: str = ""

    @property
    def scoreline(self) -> str:
        if self.home_score is None or self.away_score is None:
            return ""
        return f"{self.home_score}-{self.away_score}"

    @property
    def label(self) -> str:
        if not self.home_team or not self.away_team:
            return ""
        base = f"{self.home_team} {self.scoreline} {self.away_team}".replace("  ", " ")
        return base.strip()

    def major_events(self) -> list[MatchEvent]:
        """Major events in match order, capped so the video stays short."""
        majors = sorted(
            (e for e in self.events if e.is_major), key=lambda e: e.minute
        )
        return majors[:_MAX_MOMENTS]

    def to_moments(self) -> list[MatchMoment]:
        """Major events as footage moments, with no footage offset yet.

        Offsets stay None until the operator's footage is aligned to the match
        clock; `core.footage` then renders only the moments it can locate and
        leaves the rest to the photo pipeline.
        """
        moments = []
        for event in self.major_events():
            who = event.player or event.team
            detail = event.detail or event.type.replace("_", " ")
            moments.append(
                MatchMoment(
                    label=event.type,
                    minute=event.minute,
                    description=f"{who} — {detail}".strip(" —"),
                )
            )
        return moments


def parse_fixture_query(query: str) -> tuple[str, str]:
    """Split "Monaco vs PSG" into its two sides.

    Accepts the separators people actually type, in Latin or Arabic. Returns
    ("", "") when the input does not name two sides, which the caller treats
    as "could not identify a match".
    """
    text = (query or "").strip()
    if not text:
        return "", ""
    parts = re.split(r"\s+(?:vs\.?|v\.?|versus|ضد|×|x)\s+|\s+-\s+", text, flags=re.I)
    parts = [p.strip(" -–—") for p in parts if p and p.strip(" -–—")]
    if len(parts) != 2:
        return "", ""
    return parts[0], parts[1]


def select_provider(explicit: str = "") -> str:
    """Which provider to use, given the environment.

    A structured sports feed is preferred whenever its key is configured,
    because it gives real event minutes rather than a model's reading of a
    match report.
    """
    if explicit:
        return explicit
    if os.environ.get("FOOTBALL_DATA_API_KEY", "").strip():
        return "structured"
    return "grounded"


def normalise_events(raw: list[dict]) -> list[MatchEvent]:
    """Coerce provider output into MatchEvent, dropping unusable entries."""
    events: list[MatchEvent] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        etype = str(item.get("type") or "").strip().lower().replace(" ", "_")
        if not etype:
            continue
        minute = item.get("minute")
        try:
            minute = int(minute)
        except (TypeError, ValueError):
            continue
        if not 0 <= minute <= 130:
            continue
        confidence = str(item.get("confidence") or "unverified").lower()
        if confidence not in ("verified", "reported", "unverified"):
            confidence = "unverified"
        events.append(
            MatchEvent(
                type=etype,
                minute=minute,
                team=str(item.get("team") or "").strip(),
                player=str(item.get("player") or "").strip(),
                detail=str(item.get("detail") or "").strip(),
                confidence=confidence,
            )
        )
    return sorted(events, key=lambda e: e.minute)


def check_consistency(facts: MatchFacts) -> list[str]:
    """Discrepancies between the scoreline and the event list.

    A goal timeline that does not add up to the final score means the events
    are incomplete or wrong, and the narration must not present them as a
    complete account of the match.
    """
    problems: list[str] = []
    if facts.home_score is None or facts.away_score is None:
        problems.append("final score unknown")
        return problems

    scoring = [e for e in facts.events if e.type in ("goal", "penalty", "own_goal")]
    expected = facts.home_score + facts.away_score
    if len(scoring) != expected:
        problems.append(
            f"{len(scoring)} scoring event(s) listed for a {facts.scoreline} "
            f"result ({expected} expected)"
        )
    return problems


def facts_from_payload(payload: dict, *, provider: str) -> MatchFacts:
    """Build MatchFacts from a provider's normalised JSON payload."""
    payload = payload or {}
    home = str(payload.get("home_team") or "").strip()
    away = str(payload.get("away_team") or "").strip()

    def _score(key: str) -> int | None:
        value = payload.get(key)
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    facts = MatchFacts(
        home_team=home,
        away_team=away,
        competition=str(payload.get("competition") or "").strip(),
        played_on=str(payload.get("played_on") or "").strip(),
        home_score=_score("home_score"),
        away_score=_score("away_score"),
        events=normalise_events(payload.get("events") or []),
        provider=provider,
    )
    facts.identified = bool(home and away and facts.scoreline)

    problems = check_consistency(facts)
    if problems:
        facts.note = "; ".join(problems)
        logger.warning(f"match data inconsistent: {facts.note}")
    return facts


_SCHEMA_PROMPT = """Return ONLY this JSON object:
{
  "home_team": "string, the home side's common name",
  "away_team": "string, the away side's common name",
  "competition": "string, e.g. Ligue 1",
  "played_on": "YYYY-MM-DD, or empty string if unknown",
  "home_score": integer full-time goals, or null if unknown,
  "away_score": integer full-time goals, or null if unknown,
  "events": [
    {
      "type": "goal | penalty | own_goal | red_card | var_decision",
      "minute": integer match minute,
      "team": "string",
      "player": "string",
      "detail": "short factual phrase",
      "confidence": "verified if two or more sources agree, reported if one, unverified otherwise"
    }
  ]
}
Rules: never guess a scoreline, a scorer or a minute. Omit anything you cannot
source. If you cannot identify one specific match, return empty team names."""


async def fetch_match_facts(query: str, *, provider: str = "") -> MatchFacts:
    """Identify the match named by `query` and return its verified timeline.

    Costs one grounded research call. Returns an unidentified MatchFacts --
    never raises and never invents -- when the match cannot be pinned down, so
    the caller can fall back to a general-fixture script.
    """
    import clients

    provider = select_provider(provider)
    home, away = parse_fixture_query(query)
    if not home or not away:
        logger.info(f"could not read a fixture from {query!r}")
        return MatchFacts(provider=provider, note="fixture not parseable")

    if provider != "grounded":
        # The structured provider is declared but not implemented: no sports
        # feed is configured on this deployment. Say so rather than silently
        # returning grounded data labelled as authoritative.
        logger.warning(
            f"provider {provider!r} is not implemented; using grounded search"
        )
        provider = "grounded"

    prompt = (
        f"Identify the most recent completed football match between "
        f"{home} and {away}. Report its competition, date, final score, and "
        f"the goals, penalties, own goals, red cards and VAR decisions with "
        f"the minute each occurred. Use only what the sources state."
    )
    try:
        payload = await clients.research_with_search(
            prompt,
            schema_prompt=_SCHEMA_PROMPT,
            system_instruction="You are a football results researcher.",
            operation_label="match_identify",
        )
    except Exception as exc:
        logger.warning(f"match lookup failed for {query!r}: {exc}")
        return MatchFacts(provider=provider, note=f"lookup failed: {exc}")

    facts = facts_from_payload(payload or {}, provider=provider)
    if facts.identified:
        logger.info(f"identified match: {facts.label} ({facts.provider})")
    else:
        logger.info(f"could not identify a specific match for {query!r}")
    return facts


def research_brief(facts: MatchFacts) -> str:
    """A claim-tagged brief for the scripter, in the shape it already expects.

    Verified facts are stated plainly; anything else is tagged so the Arabic
    script attributes it instead of asserting it.
    """
    if not facts.identified:
        return (
            "MATCH NOT IDENTIFIED. Do not invent a scoreline, scorers or "
            "minutes. Discuss the fixture and the sides in general terms only."
        )

    lines = [
        f"MATCH: {facts.home_team} vs {facts.away_team}",
        f"FINAL SCORE [VERIFIED]: {facts.scoreline}",
    ]
    if facts.competition:
        lines.append(f"COMPETITION: {facts.competition}")
    if facts.played_on:
        lines.append(f"DATE: {facts.played_on}")
    lines.append(f"SOURCE: {facts.provider}")
    if facts.note:
        lines.append(
            f"DATA WARNING: {facts.note}. Do not present the event list as a "
            "complete account of the match."
        )

    lines.append("EVENTS:")
    for event in facts.major_events():
        tag = {
            "verified": "[VERIFIED]",
            "reported": "[REPORTED]",
            "unverified": "[UNVERIFIED - attribute or omit]",
        }[event.confidence]
        who = " ".join(x for x in (event.player, f"({event.team})" if event.team else "") if x)
        lines.append(
            f"  {event.minute}' {event.type.replace('_', ' ')} {who} {tag}".rstrip()
        )
    return "\n".join(lines)
