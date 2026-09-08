"""Which club a named player is at *now*, and which clubs are history.

Training data answers this question confidently and wrongly. Julián Álvarez
left Manchester City for Atlético Madrid in August 2024; a model writing from
memory still puts him at City, and a script that says so is not a small error
-- it is the whole premise of a transfer story being wrong.

News reporting alone is not enough either. A headline saying "Barcelona
hopeful of signing Julián Álvarez" names three clubs and only one of them is
his. Deciding which is which from prose is guesswork.

So the club comes from a squad and transfer record where one is available:

    /players/profiles?search=<surname>   -> the player's id
    /transfers?player=<id>               -> every move, newest first
    /players/squads?player=<id>          -> the squads he is registered in

The most recent transfer's incoming club is the current one; every other club
in the history is former. Squad membership confirms it. Reporting is then used
to cross-check, never to overrule -- a rumour that a move is agreed does not
move a player.

Where no data provider is configured this returns None rather than a guess,
and the caller falls back to dated reporting.
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

import httpx

from core.news_sources import _key

logger = logging.getLogger("video_factory")

_API_BASE = "https://v3.football.api-sports.io"
_TIMEOUT = 15.0

# National sides are squads too. A player's "club" is never one of these, and
# the transfer record is what separates them.
_NATIONAL_TEAM_RE = re.compile(
    r"\b(U\d{2}|Under[- ]\d{2})\b|^(?:[A-Z][a-z]+)(?: [A-Z][a-z]+)?$"
)


@dataclass
class PlayerStatus:
    """What is actually established about a player's club situation."""

    name: str
    player_id: int | None = None
    current_club: str = ""
    former_clubs: list[str] = field(default_factory=list)
    interested_clubs: list[str] = field(default_factory=list)
    source: str = ""
    last_transfer_date: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.current_club)

    def is_former(self, club: str) -> bool:
        if self.is_current(club):
            return False
        return any(_same_club(club, c) for c in self.former_clubs)

    def is_current(self, club: str) -> bool:
        return _same_club(club, self.current_club)


# The provider spells the same club more than one way -- "Manchester United"
# in a squad record, "Manchester Utd" in a transfer row. Left alone, a
# player's current club also appears in his former list under the other
# spelling, and a perfectly correct claim gets rejected as out of date.
_CLUB_ALIASES = {
    "utd": "united",
    "man": "manchester",
    "atl": "atletico",
    "psg": "paris saint germain",
}
_CLUB_NOISE = {"fc", "afc", "cf", "sc", "ac", "club", "the"}


def _club_key(name: str) -> str:
    """A club's identity, independent of which spelling the provider used."""
    words = [
        _CLUB_ALIASES.get(word, word)
        for word in _norm(name).split()
        if word not in _CLUB_NOISE
    ]
    return " ".join(words)


def _same_club(a: str, b: str) -> bool:
    return bool(a) and bool(b) and _club_key(a) == _club_key(b)


def _norm(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return " ".join(
        "".join(c if c.isalnum() else " " for c in stripped.lower()).split()
    )


async def _get(client: httpx.AsyncClient, path: str, params: dict, api_key: str) -> dict:
    response = await client.get(
        f"{_API_BASE}/{path}", params=params, headers={"x-apisports-key": api_key}
    )
    response.raise_for_status()
    body = response.json()
    errors = body.get("errors")
    # The provider reports refusals in the body with HTTP 200. A list is its
    # "no errors" shape; a non-empty dict is a real refusal.
    if isinstance(errors, dict) and errors:
        raise RuntimeError("; ".join(f"{k}: {v}" for k, v in errors.items()))
    return body


def _matches(candidate: dict, wanted: str) -> bool:
    """Whether this profile is the player asked for, accents ignored."""
    parts = [
        candidate.get("name"),
        f"{candidate.get('firstname') or ''} {candidate.get('lastname') or ''}",
    ]
    target = _norm(wanted)
    target_words = set(target.split())
    for part in parts:
        got = _norm(part)
        if not got:
            continue
        if got == target:
            return True
        # "Julian Alvarez" against "Julián Álvarez": every word of the query
        # must appear, so "Alvarez" alone does not match a different Álvarez.
        if target_words and target_words <= set(got.split()):
            return True
    return False


async def resolve_player_by_terms(
    display_name: str,
    search_terms: list[str],
    *,
    confirm_words: tuple[str, ...] = (),
) -> PlayerStatus | None:
    """Resolve a player from several spellings of the name.

    Arabic does not write short vowels, so a transliteration is a set of
    guesses rather than one string -- "martinez" and "martiniz" are equally
    faithful readings of مارتينيز and only one is a footballer. Each is tried
    until the provider recognises one.

    `confirm_words` disambiguates the result: a surname prefix like "martin"
    matches hundreds of players, so the forename readings are used to pick
    the right one. Without a confirmation the broadest terms are skipped
    rather than guessed at.
    """
    api_key = _key("api_football_key")
    terms = [t for t in (search_terms or []) if len(str(t)) >= 4]
    if not api_key or not terms:
        return None

    confirm = {_norm(w) for w in confirm_words if _norm(w)}

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_TIMEOUT, connect=6.0)) as client:
            player = None
            for term in terms:
                try:
                    profiles = await _get(
                        client, "players/profiles", {"search": term}, api_key
                    )
                except Exception as exc:
                    logger.debug(f"profile search {term!r} failed: {exc}")
                    continue

                entries = [e.get("player") or {} for e in profiles.get("response") or []]
                if not entries:
                    continue

                # A term that matched a great many players is a prefix, not a
                # name. Only a forename confirmation can pick one out.
                if len(entries) > 5 and not confirm:
                    continue

                for candidate in entries:
                    full = _norm(
                        f"{candidate.get('firstname') or ''} "
                        f"{candidate.get('lastname') or ''} "
                        f"{candidate.get('name') or ''}"
                    )
                    if not full:
                        continue
                    # Both halves of the name must be present. Matching on one
                    # picked Lautaro Martínez for "إيميليانو مارتينيز", and an
                    # Albanian Emiliano for the forename alone -- a confident
                    # answer about the wrong person is worse than none.
                    if not any(surname in full for surname in terms):
                        continue
                    if confirm and not any(word in full for word in confirm):
                        continue
                    player = candidate
                    break
                if player:
                    logger.info(
                        f"{display_name!r} matched {player.get('name')!r} "
                        f"via search term {term!r}"
                    )
                    break

            if not player:
                logger.info(f"No squad record found for {display_name!r}")
                return None

            return await _build_status(
                client, player, str(player.get("name") or display_name), api_key
            )
    except Exception as exc:
        logger.warning(f"Squad record unavailable for {display_name!r}: {exc}")
        return None


async def resolve_player(name: str) -> PlayerStatus | None:
    """The player's current club and club history, or None.

    None means "not established" -- no key, no match, or a provider refusal.
    It never means "no club": an unresolved player must fall back to dated
    reporting rather than to whatever the model remembers.
    """
    api_key = _key("api_football_key")
    wanted = " ".join(str(name or "").split())
    if not api_key or not wanted:
        return None

    surname = wanted.split()[-1]
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(_TIMEOUT, connect=6.0)) as client:
            profiles = await _get(
                client, "players/profiles", {"search": _norm(surname)}, api_key
            )
            player = None
            for entry in profiles.get("response") or []:
                candidate = entry.get("player") or {}
                if _matches(candidate, wanted):
                    player = candidate
                    break
            if not player:
                logger.info(f"No squad record found for {wanted!r}")
                return None

            return await _build_status(
                client, player, str(player.get("name") or wanted), api_key
            )
    except Exception as exc:
        logger.warning(f"Squad record unavailable for {wanted!r}: {exc}")
        return None


async def _build_status(
    client: httpx.AsyncClient, player: dict, display_name: str, api_key: str
) -> PlayerStatus | None:
    """Current club and history for a matched profile."""
    player_id = player.get("id")
    transfers_body, squads_body = await asyncio.gather(
        _get(client, "transfers", {"player": player_id}, api_key),
        _get(client, "players/squads", {"player": player_id}, api_key),
    )

    status = PlayerStatus(
        name=display_name,
        player_id=player_id,
        source="api-football",
    )

    moves: list[dict] = []
    for record in transfers_body.get("response") or []:
        moves.extend(record.get("transfers") or [])
    # Newest first. The most recent incoming club is where he plays now.
    moves.sort(key=lambda m: str(m.get("date") or ""), reverse=True)

    seen: set[str] = set()
    for index, move in enumerate(moves):
        teams = move.get("teams") or {}
        joined = str((teams.get("in") or {}).get("name") or "").strip()
        left = str((teams.get("out") or {}).get("name") or "").strip()
        if index == 0 and joined:
            status.current_club = joined
            status.last_transfer_date = str(move.get("date") or "")
        for club in (joined, left):
            if club and not _same_club(club, status.current_club):
                if _club_key(club) not in seen:
                    seen.add(_club_key(club))
                    status.former_clubs.append(club)

    # Squad membership confirms the transfer record. Where they disagree the
    # squad wins: it is the current registration rather than the last move.
    club_squads = [
        str((entry.get("team") or {}).get("name") or "")
        for entry in squads_body.get("response") or []
    ]
    known = {_club_key(status.current_club), *(_club_key(c) for c in status.former_clubs)}
    for team in club_squads:
        if _club_key(team) in known and not status.is_current(team):
            logger.info(
                f"{status.name}: squad record puts him at {team}, "
                f"overriding the last transfer ({status.current_club})"
            )
            previous = status.current_club
            status.current_club = team
            status.former_clubs = [
                c for c in status.former_clubs if not _same_club(c, team)
            ]
            if previous and not _same_club(previous, team):
                status.former_clubs.insert(0, previous)
            break

    if not status.resolved:
        return None

    logger.info(
        f"{status.name}: current club {status.current_club}"
        f"{f' (since {status.last_transfer_date})' if status.last_transfer_date else ''}; "
        f"former: {', '.join(status.former_clubs) or 'none recorded'}"
    )
    return status


# ── cross-checking reporting against the record ──────────────────────────

# Wording that says a club is where the player is, rather than where he might
# go. Used to read a current club out of reporting when no record is
# available, and to spot reporting that contradicts one that is.
# These run against _norm() output, which has had every apostrophe and full
# stop turned into a space -- so "Manchester City's Álvarez" arrives as
# "manchester city s alvarez". Matching the raw possessive here silently
# matched nothing, which let the single most important claim through:
# "Manchester City's Julian Alvarez" was accepted as current.
#
# Distances are counted in words rather than characters for the same reason:
# there are no sentence boundaries left to anchor to.
_CURRENT_CLUB_TEMPLATES = (
    # "manchester city s julian alvarez" -- the possessive.
    r"\b{club}\s+s\s+(?:\w+\s+){{0,2}}{player}\b",
    # "atletico madrid striker julian alvarez"
    r"\b{club}\s+(?:striker|forward|winger|midfielder|defender|goalkeeper|"
    r"keeper|star|player|captain|man)\s+(?:\w+\s+){{0,2}}{player}\b",
    # "julian alvarez of atletico madrid"
    r"\b{player}(?:\s+\w+){{0,4}}\s+(?:from|of|at)\s+{club}\b",
    # "alvarez remains at atletico madrid"
    r"\b{player}(?:\s+\w+){{0,6}}\s+(?:stay|stays|remain|remains)\s+"
    r"(?:at\s+)?{club}\b",
)

# Wording that says a club *wants* the player. Never a current club: a bid is
# not a signing, and "hopeful of signing" is the opposite of having signed.
_INTEREST_TEMPLATES = (
    r"\b{club}(?:\s+\w+){{0,8}}\s+(?:interest|interested|hopeful|target|"
    r"targeting|pursue|pursuing|bid|offer|signing)(?:\s+\w+){{0,6}}\s+{player}\b",
    r"\b{player}(?:\s+\w+){{0,8}}\s+(?:linked with|to join|move to|"
    r"transfer to)\s+{club}\b",
)


def _club_mentions(text: str, player: str, clubs: list[str], templates) -> set[str]:
    haystack = _norm(text)
    person = _norm(player).split()[-1] if player else ""
    found: set[str] = set()
    for club in clubs:
        club_pattern = re.escape(_norm(club))
        for template in templates:
            pattern = template.format(club=club_pattern, player=re.escape(person))
            if re.search(pattern, haystack):
                found.add(club)
                break
    return found


def cross_check(status: PlayerStatus, evidence: list, clubs: list[str]) -> dict:
    """Compare the squad record against what the reporting says.

    Reporting never overrules the record -- a club being "hopeful of signing"
    a player has not signed him. What this produces is the interested clubs
    worth naming, and a flag when reporting positively contradicts the record,
    which is worth a human's attention rather than a silent override.
    """
    corpus = " . ".join(
        f"{getattr(item, 'title', '')} {getattr(item, 'snippet', '')}"
        for item in evidence
    )
    interested = _club_mentions(corpus, status.name, clubs, _INTEREST_TEMPLATES)
    stated_current = _club_mentions(corpus, status.name, clubs, _CURRENT_CLUB_TEMPLATES)

    # His own club is not "interested" in him. Compared by club identity, not
    # by string: the corpus writes "Atlético Madrid" where the record says
    # "Atletico Madrid", and the accent alone was enough to list his current
    # club among the suitors.
    status.interested_clubs = sorted(
        club for club in interested if not status.is_current(club)
    )

    contradictions = sorted(
        club for club in stated_current if not status.is_current(club)
    )
    if contradictions:
        logger.warning(
            f"{status.name}: reporting places him at {', '.join(contradictions)} "
            f"but the squad record says {status.current_club}; keeping the record"
        )
    return {
        "confirms_current": status.current_club in stated_current,
        "contradictions": contradictions,
        "interested": status.interested_clubs,
    }


# ── the gate ─────────────────────────────────────────────────────────────


def outdated_claim_reason(claim: str, status: PlayerStatus) -> str:
    """Why this claim is out of date, or "".

    Catches the failure that matters: a former club written as the current
    one. "Manchester City's Julian Alvarez" and "Alvarez of Manchester City"
    are rejected; "Alvarez left Manchester City in 2024" and "former
    Manchester City forward" are not -- history stated as history is correct
    and is what makes a transfer story readable.
    """
    if not status.resolved or not str(claim or "").strip():
        return ""

    haystack = _norm(claim)
    person = _norm(status.name).split()[-1]
    if person and person not in haystack:
        return ""

    for club in status.former_clubs:
        if status.is_current(club):
            continue  # an alias of the current club is not out of date
        club_norm = _norm(club)
        if club_norm not in haystack:
            continue
        # History stated as history is correct, and is what makes a transfer
        # story readable. Only a former club written as the current one is a
        # problem.
        escaped = re.escape(club_norm)
        history_patterns = (
            # "former Manchester City forward", "left Manchester City"
            rf"\b(?:former|ex|previous|previously|once|used to|left|departed|"
            rf"sold by|academy)\b(?:\s+\w+){{0,6}}\s+{escaped}\b",
            # "joined Atletico Madrid FROM Manchester City" -- the club after
            # "from" is the one he came out of, not the one he is at.
            rf"\b(?:joined|moved|signed|arrived|came|switched|transferred)\b"
            rf"(?:\s+\w+){{0,5}}\s+from\s+{escaped}\b",
            # "at Manchester City in 2022", "before his move"
            rf"{escaped}(?:\s+\w+){{0,6}}\s+\b(?:before|until|back in|"
            rf"previously)\b",
            rf"{escaped}(?:\s+\w+){{0,4}}\s+in\s+\d{{4}}\b",
        )
        if any(re.search(p, haystack) for p in history_patterns):
            continue
        for template in _CURRENT_CLUB_TEMPLATES:
            if re.search(
                template.format(club=re.escape(club_norm), player=re.escape(person)),
                haystack,
            ):
                return (
                    f"names {club} as the player's club; he moved to "
                    f"{status.current_club}"
                    f"{f' in {status.last_transfer_date[:4]}' if status.last_transfer_date else ''}"
                )
    return ""


def premise_conflict(asserted_clubs: list[str], statuses: list[PlayerStatus]) -> str:
    """Why the topic's own premise is false, or "".

    A planner picked "Emiliano Martínez after his transfer from Aston Villa to
    Chelsea". He has never played for Chelsea. Nothing downstream could save
    that: the script is about a move that did not happen, and no authentic
    photograph of him in a Chelsea shirt exists, so sourcing returns fan-art
    and the run dies six minutes later having written the whole thing.

    A destination the record does not support is reported here, before a word
    is written. Where the player cannot be resolved this stays silent -- an
    unverified premise is not the same as a false one.
    """
    if not asserted_clubs:
        return ""
    for status in statuses:
        if not status.resolved:
            continue
        for club in asserted_clubs:
            if status.is_current(club):
                return ""  # the premise agrees with the record
            if status.is_former(club):
                return (
                    f"the topic presents {club} as {status.name}'s destination, "
                    f"but that is a club he has already left; he is at "
                    f"{status.current_club}"
                )
            return (
                f"the topic presents {status.name} as having moved to {club}, "
                f"but the squad record has him at {status.current_club}"
                f"{f' since {status.last_transfer_date}' if status.last_transfer_date else ''}"
                f" and shows no transfer to {club}"
            )
    return ""


def render_current_facts(statuses: list[PlayerStatus]) -> str:
    """The established facts, as a block the script must not contradict."""
    resolved = [s for s in statuses if s.resolved]
    if not resolved:
        return ""
    lines = [
        "ESTABLISHED SQUAD FACTS (structured data, authoritative — the script "
        "and every image brief must agree with these):"
    ]
    for status in resolved:
        line = f"- {status.name} plays for {status.current_club}"
        if status.last_transfer_date:
            line += f" (since {status.last_transfer_date})"
        lines.append(line + ".")
        if status.former_clubs:
            lines.append(
                f"    Former clubs, history only — never write these as his "
                f"current club: {', '.join(status.former_clubs)}."
            )
        if status.interested_clubs:
            lines.append(
                f"    Reported interest from {', '.join(status.interested_clubs)} "
                f"— interest is not a transfer."
            )
    return "\n".join(lines)
