"""Stage 0b: web-grounded news research.

Runs between planning and scripting. The planner picks a subject; this stage
establishes what is actually true about it *right now*, using Google Search
grounding, and hands the scripter a verified brief.

Without it the model writes from training data, which for football news is
routinely months stale — describing a completed transfer as "a possible move",
or reporting a manager who has since been sacked as still in charge.

The brief is written into `plan["research_context"]`, which the script prompt
already consumes.
"""

import json
import logging
import re
from datetime import date, timedelta

import clients
import prompts
from core import arabic_names, news_sources, player_facts
from core.utils import ChannelConfig

# Clubs named often enough in football writing to anchor a cross-check.
_KNOWN_CLUBS = (
    "Barcelona", "Real Madrid", "Atletico Madrid", "Atlético Madrid",
    "Arsenal", "Manchester United", "Manchester City", "Liverpool",
    "Chelsea", "Tottenham", "Newcastle", "Aston Villa", "West Ham",
    "Bayern Munich", "Borussia Dortmund", "Juventus", "Inter Milan",
    "AC Milan", "Napoli", "Roma", "Paris Saint-Germain", "PSG",
    "Ajax", "Porto", "Benfica", "River Plate", "Boca Juniors",
    "Al Hilal", "Al Nassr", "Al-Hilal", "Al-Nassr",
)

# A person's name in a topic: two or three capitalised words in a row, with
# the club vocabulary above removed so "Real Madrid" is not read as a player.
_NAME_RUN_RE = re.compile(r"\b([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){1,2})\b")


# Capitalised because a sentence starts there, not because it is a name.
# "…Julian Alvarez. Is Barcelona pursuing…" produced the candidate "Julian
# Alvarez Is", whose surname "Is" is too short for the provider to search on.
_SENTENCE_STARTERS = frozenset(
    """is are was were the a an and or but if why how what when where who
    does do did has have had will would can could should this that these
    those his her their our your it he she they we you i in on at for from
    with about after before during""".split()
)


def candidate_player_names(text: str) -> list[str]:
    """Names in *text* that could be players, clubs excluded."""
    club_words = {w.lower() for club in _KNOWN_CLUBS for w in club.split()}
    names: list[str] = []
    for match in _NAME_RUN_RE.finditer(str(text or "")):
        words = " ".join(match.group(1).split()).split()
        # Trim leading and trailing sentence words rather than dropping the
        # whole run: the name is usually still in there.
        while words and words[0].lower() in _SENTENCE_STARTERS:
            words.pop(0)
        while words and words[-1].lower() in _SENTENCE_STARTERS:
            words.pop()
        if len(words) < 2:
            continue
        if any(w.lower() in club_words for w in words):
            continue
        phrase = " ".join(words)
        if phrase not in names:
            names.append(phrase)
    return names

logger = logging.getLogger("video_factory")


def _is_news_channel(config: ChannelConfig) -> bool:
    """Whether this channel reports on things that change week to week.

    Gated on the niche rather than a new config field: `core/utils.py` is
    being edited elsewhere, and the football channel is already the only one
    whose category says "news". A storytelling channel gets none of this --
    its subject matter does not go stale, and it should not pay for news
    lookups it cannot use.
    """
    category = str(getattr(config.niche, "category", "") or "").lower()
    focus = str(getattr(config.niche, "focus", "") or "").lower()
    return "news" in category or "football" in category or "football" in focus


_LATIN_DESTINATION_RE = re.compile(
    r"\b(?:to|joins?|joined|signs? for|move to|transfer to|switch to)\s+"
    r"([A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){0,2})",
)


def _latin_destination_clubs(text: str) -> list[str]:
    """Clubs a Latin-script topic presents as the player's destination."""
    known = {c.lower(): c for c in _KNOWN_CLUBS}
    found: list[str] = []
    for match in _LATIN_DESTINATION_RE.finditer(str(text or "")):
        phrase = " ".join(match.group(1).split())
        canonical = known.get(phrase.lower())
        if canonical and canonical not in found:
            found.append(canonical)
    return found


async def _resolve_named_players(text: str, evidence: list) -> list:
    """Squad facts for every player the topic names, cross-checked.

    Never raises and never guesses: a player who cannot be resolved is simply
    absent from the result, and the caller falls back to dated reporting.
    """
    lookups: list[tuple[str, list[str], tuple[str, ...]]] = []

    # Arabic first: this channel writes in it, and capitalised-run detection
    # is a Latin-script idea that found nothing at all in an Arabic topic --
    # which is how an invented Chelsea transfer reached sourcing unchecked.
    if arabic_names.contains_arabic(text):
        for candidate in arabic_names.person_name_candidates(text):
            lookups.append(
                (
                    candidate["arabic"],
                    candidate["search_terms"],
                    tuple(candidate["confirm_terms"]),
                )
            )

    for name in candidate_player_names(text):
        lookups.append((name, [name.split()[-1]], tuple(name.split()[:-1])))

    if not lookups:
        return []

    resolved: list = []
    for display, terms, confirm in lookups[:4]:
        try:
            status = await player_facts.resolve_player_by_terms(
                display, terms, confirm_words=confirm
            )
        except Exception as exc:
            logger.warning(f"Could not resolve {display!r}: {exc}")
            continue
        if not status:
            continue
        try:
            player_facts.cross_check(status, evidence, list(_KNOWN_CLUBS))
        except Exception as exc:
            logger.warning(f"Cross-check failed for {name!r}: {exc}")
        resolved.append(status)
    return resolved


def _reject_outdated_claims(research: dict, statuses: list) -> int:
    """Drop claims that state a former club as the player's current one.

    Removed outright rather than downgraded: a downgraded wrong club is still
    a wrong club on screen, and the script prompt carries the correct one.
    """
    facts = research.get("verified_facts")
    if not statuses or not isinstance(facts, list):
        return 0

    kept: list = []
    rejected = 0
    for fact in facts:
        claim = fact.get("claim", "") if isinstance(fact, dict) else str(fact)
        reason = ""
        for status in statuses:
            reason = player_facts.outdated_claim_reason(claim, status)
            if reason:
                break
        if reason:
            rejected += 1
            logger.error(
                f"Claim rejected as out of date — {reason}: {str(claim)[:110]}"
            )
            continue
        kept.append(fact)

    if rejected:
        research["verified_facts"] = kept
        research["claims_rejected_outdated"] = rejected
    return rejected


async def _retrieve_current_reporting(*, topic: str, angle: str) -> list:
    """Dated articles about the topic from the configured news providers.

    Never raises: research is an enhancement, and losing it must not take down
    a run that can still produce a video.
    """
    try:
        items = await news_sources.fetch_current_news(
            topic,
            trusted_domains=_TRUSTED_SOURCE_DOMAINS,
            days=_STALE_AFTER_DAYS,
        )
    except Exception as exc:
        logger.warning(f"Live news retrieval failed ({exc}); continuing without it")
        return []

    live = [name for name, on in news_sources.provider_status().items() if on]
    if items:
        newest = items[0].age_days()
        logger.info(
            f"Retrieved {len(items)} current article(s) from {', '.join(live) or 'no'} "
            f"provider(s); newest {newest if newest is not None else '?'}d old"
        )
    else:
        logger.warning(
            f"No current reporting found for {topic!r} via {', '.join(live) or 'no'} "
            f"provider(s) — the script will not be allowed to invent one"
        )
    return items


# Words too common in football writing to show that two texts are about the
# same thing.
_CLAIM_STOPWORDS = frozenset("""
the a an of to in on at for from with and or is are was were be been has have
had will would could may might club team player transfer deal move sign signed
signing new his her their this that it as by after before now says said report
reports reported linked interest talks agreement fee contract season
agree agrees agreed join joins joined joining reveal reveals revealed
set close near expected confirm confirms confirmed complete completes completed
""".split())


def _claim_tokens(text: str) -> set[str]:
    """The distinctive words in a claim: names, clubs, places, numbers."""
    cleaned = "".join(c if c.isalnum() or c.isspace() else " " for c in str(text or "").lower())
    return {
        word
        for word in cleaned.split()
        if len(word) > 2 and word not in _CLAIM_STOPWORDS
    }


def _verify_claims_against_evidence(research: dict, evidence: list) -> None:
    """Downgrade claims the retrieved reporting does not support.

    Only ever downgrades. A model claim stated as COMPLETED that no retrieved
    article mentions becomes REPORTED and is marked unsupported -- the
    scripter's rules already stop it narrating a REPORTED claim as fact. This
    never promotes a claim, because agreeing with a headline is not
    verification and treating it as such would manufacture false confidence.

    With no evidence retrieved there is nothing to check against, so claims
    are left exactly as the model returned them.
    """
    facts = research.get("verified_facts")
    if not evidence or not isinstance(facts, list):
        return

    corpus = [
        _claim_tokens(f"{item.title} {item.snippet}") for item in evidence
    ]
    downgraded = 0
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        status = str(fact.get("status") or "").upper()
        if status not in {"COMPLETED", "CONFIRMED"}:
            continue
        tokens = _claim_tokens(fact.get("claim", ""))
        if not tokens:
            continue
        # Supported when a single article shares most of the claim's
        # distinctive words -- the same names and clubs in one place.
        supported = any(
            len(tokens & article) / len(tokens) >= 0.5 for article in corpus
        )
        if supported:
            continue
        fact["status"] = "REPORTED"
        fact["unsupported_by_retrieved_reporting"] = True
        downgraded += 1
        logger.warning(
            f"Claim downgraded to REPORTED — no retrieved article supports it: "
            f"{str(fact.get('claim', ''))[:110]}"
        )

    if downgraded:
        research["claims_downgraded"] = downgraded


def _player_status_rows(statuses: list) -> list[dict]:
    """The resolved squad facts, as plain data the later stages can read."""
    return [
        {
            "name": s.name,
            "current_club": s.current_club,
            "former_clubs": s.former_clubs,
            "interested_clubs": s.interested_clubs,
            "source": s.source,
        }
        for s in statuses
        if s.resolved
    ]


def _research_from_evidence(
    evidence: list, *, reason: str, statuses: list | None = None
) -> dict:
    """A brief built only from retrieved articles, with no model claims.

    Used when the model's own answer is thrown away. The articles are real,
    dated and attributable, so the scripter still writes from current
    reporting instead of from memory -- which is what an empty brief left it
    doing. Every claim is marked REPORTED: these are headlines, and nothing
    here has been cross-checked into a stronger status.
    """
    facts_block = player_facts.render_current_facts(statuses or [])
    if not evidence:
        # Squad facts stand on their own. The model's answer being unusable
        # does not make "which club does he play for" unknown, and this path
        # is exactly where a script would otherwise be written from memory.
        return {
            "brief": facts_block,
            "sources": [],
            "key_entities": [],
            "grounded": bool(facts_block),
            "rejected_reason": reason,
        }

    facts = [
        {
            "claim": item.title,
            "status": "REPORTED",
            "source": item.domain,
            "url": item.url,
            "published": item.published_at,
        }
        for item in evidence
    ]
    brief = (
        "VERIFIED AGAINST CURRENT REPORTING ONLY.\n"
        f"The model's own research was discarded ({reason}), so the following "
        "is drawn strictly from articles retrieved just now. Do not state "
        "anything beyond what these lines support.\n\n"
        + news_sources.render_evidence(evidence)
    )
    if facts_block:
        brief = f"{facts_block}\n\n{brief}"
    logger.warning(
        f"Model research discarded ({reason}); falling back to "
        f"{len(evidence)} retrieved article(s) rather than an empty brief"
    )
    return {
        "brief": brief,
        "sources": [{"title": i.title, "url": i.url, "domain": i.domain} for i in evidence],
        "key_entities": [],
        "verified_facts": facts,
        "grounded": True,
        "evidence_only": True,
        "rejected_reason": reason,
        # Carried on this path too. It is the path the Álvarez topic actually
        # takes, and without it visual sourcing has no current club to hold
        # the image briefs to.
        "player_status": _player_status_rows(statuses or []),
    }

# How a claim may be labelled. The scripter must carry these through so the
# narration never states a rumour as fact.
CLAIM_STATUSES = (
    "COMPLETED",
    "CONFIRMED",
    "NEGOTIATING",
    "REPORTED",
    "RUMORED",
)


def _format_brief(research: dict) -> str:
    """Render the research JSON into the text block the script prompt takes."""
    lines: list[str] = []

    headline = str(research.get("headline_status", "")).strip()
    if headline:
        if research.get("structuring_failed"):
            # Unstructured but grounded: hand the scripter the findings as-is,
            # clearly labelled so it does not over-trust the phrasing.
            lines.append("WEB RESEARCH FINDINGS (verified against live sources):")
            lines.append(headline)
        else:
            lines.append(f"CURRENT STATUS OF THIS STORY: {headline}")

    latest = str(research.get("latest_development", "")).strip()
    if latest:
        as_of = str(research.get("as_of_date", "")).strip()
        lines.append(f"LATEST DEVELOPMENT{f' (as of {as_of})' if as_of else ''}: {latest}")

    facts = research.get("verified_facts") or []
    if facts:
        lines.append("")
        lines.append("VERIFIED FACTS — each is tagged with how well established it is.")
        lines.append("Narration MUST respect these tags: state CONFIRMED/COMPLETED facts")
        lines.append("plainly, and attribute REPORTED/RUMORED ones as reports.")
        for fact in facts[:12]:
            if isinstance(fact, dict):
                status = str(fact.get("status", "REPORTED")).upper()
                claim = str(fact.get("claim", "")).strip()
                source = str(fact.get("source", "")).strip()
                if claim:
                    lines.append(
                        f"  - [{status}] {claim}" + (f" (source: {source})" if source else "")
                    )
            elif str(fact).strip():
                lines.append(f"  - [REPORTED] {fact}")

    stale = research.get("outdated_claims_to_avoid") or []
    if stale:
        lines.append("")
        lines.append("DO NOT SAY — these were true once but are now wrong:")
        for claim in stale[:8]:
            if str(claim).strip():
                lines.append(f"  - {claim}")

    entities = research.get("key_entities") or []
    if entities:
        lines.append("")
        lines.append("KEY ENTITIES — use these exact names in image search keywords:")
        for ent in entities[:14]:
            if isinstance(ent, dict):
                name = str(ent.get("name", "")).strip()
                kind = str(ent.get("type", "")).strip()
                if name:
                    lines.append(f"  - {name}" + (f" ({kind})" if kind else ""))
            elif str(ent).strip():
                lines.append(f"  - {ent}")

    sources = research.get("sources") or []
    if sources:
        titles = [
            str(s.get("title", "")).strip() if isinstance(s, dict) else str(s).strip()
            for s in sources[:8]
        ]
        titles = [x for x in titles if x]
        if titles:
            lines.append("")
            lines.append("SOURCES CONSULTED: " + ", ".join(titles))

    return "\n".join(lines).strip()


async def research_topic(
    *,
    topic: str,
    angle: str,
    config: ChannelConfig,
) -> dict:
    """Research `topic` against live web sources.

    Returns the parsed research dict with a rendered `brief` added. Never
    raises: research failing must not take down a run that can still produce a
    reasonable video, so an empty brief is returned and the scripter simply
    proceeds without it.
    """
    logger.info(f"Researching: {topic!r}")

    prompt = prompts.news_research_prompt(
        topic=topic,
        angle=angle,
        language=config.language,
        niche_focus=config.niche.focus,
        today=date.today().isoformat(),
        statuses=CLAIM_STATUSES,
    )

    # Retrieve the reporting before asking the model anything. Grounded search
    # refuses to answer without citations, but it cannot promise there will be
    # any -- a run had its research discarded because the model's own search
    # returned YouTube, Facebook and Reddit, and the script was then written
    # from nothing, which is training data by another name. These articles are
    # dated, attributable, and appended to the prompt as the evidence the
    # answer has to agree with.
    evidence: list[news_sources.NewsItem] = []
    statuses: list[player_facts.PlayerStatus] = []
    if _is_news_channel(config):
        evidence = await _retrieve_current_reporting(topic=topic, angle=angle)

        # Which club each named player is at *now*, from a squad and transfer
        # record rather than from memory or from a headline. A model writing
        # from training data still has Julián Álvarez at Manchester City; he
        # moved to Atlético Madrid in August 2024, and a transfer story built
        # on the wrong club is wrong from its first line.
        statuses = await _resolve_named_players(f"{topic} {angle}", evidence)
        facts_block = player_facts.render_current_facts(statuses)
        if facts_block:
            prompt = f"{prompt}\n\n{facts_block}"

        # Whether the topic's own premise survives the record. A planner can
        # invent a transfer, and everything downstream then works faithfully
        # on a false story.
        asserted = arabic_names.destination_clubs(topic) if arabic_names.contains_arabic(
            topic
        ) else _latin_destination_clubs(topic)
        premise = player_facts.premise_conflict(asserted, statuses)
        if premise:
            logger.error(
                f"Topic premise contradicted by the squad record — {premise}. "
                f"Refusing to research or script it."
            )
            return {
                "brief": "",
                "sources": [],
                "key_entities": [],
                "grounded": False,
                "premise_conflict": premise,
                "player_status": _player_status_rows(statuses),
            }

        if evidence:
            prompt = (
                f"{prompt}\n\n"
                f"{news_sources.render_evidence(evidence)}\n\n"
                "Ground every claim in the reporting above. Where it "
                "contradicts what you recall, the reporting is right and your "
                "recollection is out of date. Do not state anything it does "
                "not support."
            )

    try:
        research = await clients.research_with_search(
            prompt,
            schema_prompt=prompts.news_research_schema_prompt(CLAIM_STATUSES),
            system_instruction=prompts.news_research_system(),
            temperature=0.2,
            operation_label="news_research",
        )
    except Exception as exc:
        logger.warning(
            f"Grounded research unavailable ({exc}); falling back to retrieved "
            f"reporting"
        )
        return _research_from_evidence(evidence, statuses=statuses, reason=f"grounded search failed: {exc}")

    # An ungrounded answer is worse than none: it reads as researched but is
    # only as current as the model's training data, which for football news is
    # routinely a year or more stale. Drop it rather than let the scripter
    # treat it as verified.
    if not research.get("grounded"):
        logger.warning(
            "Research produced no search citations — discarding it rather than "
            "trusting stale training data"
        )
        return _research_from_evidence(evidence, statuses=statuses, reason="no search citations")

    fabricated = _reject_fabricated_research(research)
    if fabricated:
        logger.error(
            f"Research discarded — {fabricated}. Falling back to retrieved "
            f"reporting rather than narrating unverifiable claims as news."
        )
        return _research_from_evidence(evidence, statuses=statuses, reason=fabricated)

    _flag_stale_research(research)
    _verify_claims_against_evidence(research, evidence)
    # Before the script is written, not after: a claim putting the player at
    # a former club would otherwise reach the scripter as a verified fact.
    _reject_outdated_claims(research, statuses)

    brief = _format_brief(research)
    # The reporting goes into the brief alongside the model's own summary, so
    # the scripter can see the dated headlines rather than only a distilled
    # claim about them.
    if evidence:
        brief = f"{brief}\n\n{news_sources.render_evidence(evidence)}".strip()
    # The squad facts lead the brief. Everything after them -- the model's
    # summary, the headlines -- is read against them, and the image briefs are
    # written from the same block, so a beat cannot name a club the player
    # left two years ago.
    facts_block = player_facts.render_current_facts(statuses)
    if facts_block:
        brief = f"{facts_block}\n\n{brief}".strip()
        research["player_status"] = _player_status_rows(statuses)
    research["brief"] = brief

    facts = research.get("verified_facts") or []
    logger.info(
        f"Research: {len(facts)} fact(s), "
        f"{len(research.get('sources') or [])} source(s), "
        f"status={research.get('headline_status', 'n/a')!r}"
    )
    for fact in facts[:6]:
        if isinstance(fact, dict):
            logger.info(
                f"  [{str(fact.get('status', '?')).upper()}] "
                f"{str(fact.get('claim', ''))[:110]}"
            )
    return research


# A story whose newest source predates this is background, not news.
_STALE_AFTER_DAYS = 45

# Outlets whose football reporting is reliable enough to state as fact. Matched
# as domain suffixes, so "www.bbc.co.uk" and "bbc.co.uk" both count.
_TRUSTED_SOURCE_DOMAINS = (
    # Wire services and broadsheets
    "bbc.co.uk", "bbc.com", "reuters.com", "apnews.com", "nytimes.com",
    "theguardian.com", "telegraph.co.uk", "independent.co.uk", "thetimes.co.uk",
    "standard.co.uk", "mirror.co.uk", "dailymail.co.uk", "express.co.uk",
    "metro.co.uk", "inews.co.uk", "liverpoolecho.co.uk", "manchestereveningnews.co.uk",
    # Sports desks
    "skysports.com", "espn.com", "espn.co.uk", "theathletic.com", "talksport.com",
    "goal.com", "football365.com", "90min.com", "fourfourtwo.com",
    "beinsports.com", "eurosport.com", "sportsnet.ca", "cbssports.com",
    "nbcsports.com", "si.com", "bleacherreport.com", "givemesport.com",
    # Governing bodies and clubs
    "premierleague.com", "uefa.com", "fifa.com", "efl.com", "thefa.com",
    "bundesliga.com", "laliga.com", "legaseriea.it", "ligue1.com",
    # Reference and data
    "transfermarkt.com", "transfermarkt.co.uk", "transfermarkt.us",
    "wikipedia.org", "whoscored.com", "sofascore.com", "fbref.com",
    # Non-English majors
    "lequipe.fr", "marca.com", "as.com", "mundodeportivo.com", "sport.es",
    "gazzetta.it", "corrieredellosport.it", "tuttosport.com", "kicker.de",
    "bild.de", "record.pt", "abola.pt", "globo.com", "ole.com.ar",
    "kooora.com", "alkass.net", "filgoal.com", "yallakora.com",
)


def _source_domains(research: dict) -> list[str]:
    """Best-effort domain list from whatever shape the sources came back in."""
    domains: list[str] = []
    for src in research.get("sources") or []:
        raw = ""
        if isinstance(src, dict):
            raw = str(src.get("url") or src.get("domain") or src.get("title") or "")
        else:
            raw = str(src)
        raw = raw.strip().lower()
        if not raw:
            continue
        # Tolerate bare domains as well as full URLs.
        raw = raw.split("//")[-1].split("/")[0].strip()
        if raw:
            domains.append(raw)
    return domains


def _trusted_source_count(research: dict) -> int:
    return sum(
        1
        for d in _source_domains(research)
        if any(d == t or d.endswith("." + t) for t in _TRUSTED_SOURCE_DOMAINS)
    )


def _reject_fabricated_research(research: dict) -> str:
    """Return a reason to discard the brief, or "" to keep it.

    Grounded search can return citations and still be fiction: a run for the
    2026 deadline day came back "grounded" with six COMPLETED transfers that
    never happened, dated tomorrow, sourced from content farms. Narrating that
    as breaking news is the worst failure this pipeline can produce, so two
    cheap structural checks run before the scripter ever sees it.
    """
    raw = str(research.get("as_of_date", "")).strip()
    if raw:
        try:
            as_of = date.fromisoformat(raw[:10])
        except ValueError:
            as_of = None
        # One day of slack, not zero. The worker's clock is local while the
        # model reports in UTC, so for the hours either side of UTC midnight a
        # perfectly current source legitimately reads as "tomorrow" -- a run at
        # 20:12 local was 03:12 UTC the next day, and a zero-slack check threw
        # away good research every evening. Anything beyond that is the model
        # describing events that have not happened.
        if as_of and as_of > date.today() + timedelta(days=1):
            return (
                f"as_of_date {raw} is more than a day ahead of today; the "
                f"model is describing events that have not happened"
            )

    trusted = _trusted_source_count(research)
    if research.get("sources") and trusted == 0:
        return (
            "no citation resolves to a recognised football news outlet "
            f"(saw: {', '.join(_source_domains(research)[:6]) or 'none'})"
        )
    return ""


def _flag_stale_research(research: dict) -> None:
    """Downgrade research whose newest source is old.

    Grounded search can still surface a years-old article — the model may
    anchor its queries to its training era. Reporting that as "breaking" is
    how a two-year-old squad update becomes today's headline, so the age is
    checked against the clock rather than trusted.
    """
    raw = str(research.get("as_of_date", "")).strip()
    if not raw:
        return
    try:
        as_of = date.fromisoformat(raw[:10])
    except ValueError:
        return

    age_days = (date.today() - as_of).days
    research["source_age_days"] = age_days
    if age_days <= _STALE_AFTER_DAYS:
        return

    logger.warning(
        f"Newest research source is {age_days} days old ({raw}); treating the "
        f"story as background rather than breaking news"
    )
    research["is_breaking"] = False
    research["confidence"] = "low"
    note = (
        f"The freshest source found is dated {raw}, {age_days} days ago. Do NOT "
        f"present this as breaking or as the latest development; write it as "
        f"background and avoid implying anything happened recently."
    )
    existing = str(research.get("headline_status", "")).strip()
    research["headline_status"] = f"{existing} [{note}]" if existing else note


def save_research(workspace, research: dict) -> None:
    """Persist the brief next to the plan so a run can be audited later."""
    path = workspace / "research.json"
    path.write_text(
        json.dumps(research, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(f"Research saved: {path.name}")
