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
from datetime import date, timedelta

import clients
import prompts
from core.utils import ChannelConfig

logger = logging.getLogger("video_factory")

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
            f"Grounded research unavailable ({exc}); the script will be written "
            f"without a verified brief"
        )
        return {"brief": "", "sources": [], "key_entities": []}

    # An ungrounded answer is worse than none: it reads as researched but is
    # only as current as the model's training data, which for football news is
    # routinely a year or more stale. Drop it rather than let the scripter
    # treat it as verified.
    if not research.get("grounded"):
        logger.warning(
            "Research produced no search citations — discarding it and writing "
            "the script without a verified brief rather than trusting stale "
            "training data"
        )
        return {"brief": "", "sources": [], "key_entities": [], "grounded": False}

    fabricated = _reject_fabricated_research(research)
    if fabricated:
        logger.error(
            f"Research discarded — {fabricated}. Writing the script without a "
            f"verified brief rather than narrating unverifiable claims as news."
        )
        return {
            "brief": "",
            "sources": [],
            "key_entities": [],
            "grounded": False,
            "rejected_reason": fabricated,
        }

    _flag_stale_research(research)

    brief = _format_brief(research)
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
