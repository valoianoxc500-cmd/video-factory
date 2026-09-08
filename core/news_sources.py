"""Current football facts from news APIs, not from model memory.

Grounded model search already refuses to answer without citations, which stops
the worst failure -- a confident answer drawn from training data. What it does
not do is guarantee there is anything to cite: when the model's search returns
YouTube, Facebook and Reddit, the citation gate correctly discards the lot and
the scripter is left writing from nothing, which is model memory by another
route. A run did exactly that, and "which club does X play for" is precisely
the question training data gets wrong.

So the facts are fetched directly here, from sources that carry a publication
date and a domain we can check:

  * NewsAPI      -- dated articles, filtered to outlets we already trust
  * Serper news  -- Google News results, same filtering
  * GNews        -- same shape again, when a key is configured
  * API-Football -- structured squad and transfer records, when a key is
                    configured; a database row beats a headline for "who does
                    this player play for right now"

Every provider is optional and independent. A missing key disables that
provider and nothing else, so this degrades to whatever is configured rather
than failing. Nothing here invents a fact: an item exists only if a provider
returned it, and each carries the URL and date it came with.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import httpx

from settings import settings

logger = logging.getLogger("video_factory")

# Long enough to catch a transfer window's reporting, short enough that a
# result is still "now". Matches the staleness rule the researcher applies.
DEFAULT_WINDOW_DAYS = 30

# One provider being slow must not hold up the others or the run.
_PROVIDER_TIMEOUT_SECONDS = 12.0

# How far back NewsAPI's free plan will answer for. Asking for more is not a
# smaller result set, it is a 426 and no results at all.
_NEWSAPI_MAX_WINDOW_DAYS = 28


@dataclass
class NewsItem:
    """One dated, attributable article."""

    title: str
    url: str
    domain: str
    published_at: str
    snippet: str = ""
    provider: str = ""

    def age_days(self, today: date | None = None) -> int | None:
        now = today or date.today()
        parsed = _parse_date(self.published_at, today=now)
        if parsed is None:
            return None
        return (now - parsed).days


@dataclass
class PlayerFacts:
    """Structured squad facts for one player, straight from a data provider."""

    name: str
    team: str = ""
    league: str = ""
    season: str = ""
    nationality: str = ""
    age: int | None = None
    position: str = ""
    provider: str = "api-football"
    transfers: list[dict] = field(default_factory=list)


# Serper reports Google News' own wording rather than a timestamp: "2 hours
# ago", "1 week ago", "May 6, 2026". Left unparsed these count as undated and
# slip past the recency filter -- a live check pulled a four-month-old article
# into a thirty-day window that way, which is the exact staleness this module
# exists to stop.
_RELATIVE_DATE_RE = re.compile(
    r"\b(\d+)\s*(minute|min|hour|hr|day|week|month|year)s?\s+ago\b", re.IGNORECASE
)
_RELATIVE_DAYS = {
    "minute": 0.0, "min": 0.0, "hour": 0.0, "hr": 0.0,
    "day": 1.0, "week": 7.0, "month": 30.44, "year": 365.25,
}
_TEXT_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y/%m/%d")

# Google News writes "Sept" where strptime's %b only accepts "Sep". Left
# unhandled the whole date is unreadable, and an unreadable date is now a
# rejected article -- so one abbreviation would quietly drop a month of
# September reporting.
_MONTH_ABBREVIATION_FIXUPS = (("sept ", "sep "), ("sept. ", "sep "))


def _parse_date(raw: str, *, today: date | None = None) -> date | None:
    text = str(raw or "").strip()
    if not text:
        return None
    now = today or date.today()

    # ISO 8601, with or without a time and a Z.
    cleaned = text.replace("Z", "+00:00")
    for attempt in (cleaned, cleaned[:10]):
        try:
            return datetime.fromisoformat(attempt).date()
        except ValueError:
            pass

    relative = _RELATIVE_DATE_RE.search(text)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(2).lower()
        return now - timedelta(days=round(amount * _RELATIVE_DAYS[unit]))

    if re.search(r"\b(just now|moments? ago|today)\b", text, re.IGNORECASE):
        return now
    if re.search(r"\byesterday\b", text, re.IGNORECASE):
        return now - timedelta(days=1)

    normalised = text
    lowered = text.lower()
    for wrong, right in _MONTH_ABBREVIATION_FIXUPS:
        if lowered.startswith(wrong) or f" {wrong}" in lowered:
            normalised = re.sub(
                re.escape(wrong.strip()), right.strip(), text, flags=re.IGNORECASE
            )
            break

    for candidate in (text, normalised):
        for fmt in _TEXT_DATE_FORMATS:
            try:
                return datetime.strptime(candidate, fmt).date()
            except ValueError:
                continue
    return None


# Words a football query is full of that say nothing about which story it is.
# A headline sharing only these with the topic is not about the topic.
_TOPIC_STOPWORDS = frozenset("""
a an the of to in on at for from with and or is are was was were be been has
have had will would could may might do does did not no
club team player striker forward midfielder defender keeper manager boss coach
transfer transfers move moves signing sign signed deal deals bid offer fee
contract loan window interest interested linked link talks news latest update
updates report reports reported rumour rumours rumor rumors saga future
situation stay exit summer winter season current now today
""".split())


def _normalise(text: str) -> str:
    """Lowercase, accent-stripped, alphanumeric.

    Accents matter here: ESPN writes "Julián Álvarez" where the topic says
    "Julian Alvarez", and that article is one of the few stating his current
    club. Comparing the raw strings would throw it away.
    """
    decomposed = unicodedata.normalize("NFKD", str(text or ""))
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return "".join(c if c.isalnum() else " " for c in stripped.lower())


def topic_terms(query: str) -> list[str]:
    """The words that identify which story the topic is about.

    Generic football vocabulary is dropped, so "Barcelona interest in Julian
    Alvarez" reduces to barcelona / julian / alvarez -- the names an article
    has to share for it to be the same story.
    """
    return [
        word
        for word in _normalise(query).split()
        if len(word) > 2 and word not in _TOPIC_STOPWORDS
    ]


def _required_matches(terms: list[str]) -> int:
    """How many topic words an article must carry. Half, rounded up."""
    return max(1, (len(terms) + 1) // 2)


def is_relevant(item: "NewsItem", terms: list[str]) -> bool:
    """Whether this article is about the topic, rather than merely nearby.

    A search for "Barcelona interest in Julian Alvarez" returns Arsenal squad
    round-ups and an unrelated Martinelli transfer: real articles from trusted
    outlets, matched on the generic half of the query. They reached the brief
    as facts about Alvarez, which is how an unrelated player ends up in his
    story. Requiring half the topic's own words keeps a piece that names the
    player without demanding it repeat the whole query.

    With no usable terms nothing is filtered: an unfiltered brief is a smaller
    problem than an empty one.
    """
    if not terms:
        return True
    haystack = _normalise(f"{item.title} {item.snippet}")
    hits = sum(1 for term in set(terms) if term in haystack)
    return hits >= _required_matches(list(set(terms)))


def _domain_of(url: str) -> str:
    try:
        host = urlparse(str(url or "")).hostname or ""
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_trusted(domain: str, trusted: tuple[str, ...]) -> bool:
    """Suffix match, so a subdomain of a trusted outlet still counts."""
    host = (domain or "").lower()
    return any(host == d or host.endswith("." + d) for d in trusted)


def _dotenv_path() -> Path | None:
    """The .env file `Settings` is configured to read, if any."""
    try:
        configured = type(settings).model_config.get("env_file")
    except Exception:
        return None
    return Path(str(configured)) if configured else None


@functools.lru_cache(maxsize=1)
def _dotenv_values() -> dict[str, str]:
    """The .env file settings itself reads, parsed for keys it has no field for.

    `Settings` declares `extra: "ignore"`, so a credential in .env that has no
    matching field is dropped silently -- which is what happened to
    API_FOOTBALL_KEY: present in the file, invisible to the app. Reading the
    same file settings points at means a new provider needs a key and nothing
    else, and there is still only one place the path is defined.
    """
    try:
        path = _dotenv_path()
        if not path or not path.is_file():
            return {}
        values: dict[str, str] = {}
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip().upper()] = value.strip().strip('"').strip("'")
        return values
    except OSError:
        return {}


def _key(name: str) -> str:
    """A configured credential, or "" when the provider is not set up.

    Checked in the order a deployment would expect: the settings field if one
    exists, then a real environment variable, then .env. The last two are what
    let a provider settings.py has no field for -- GNews and API-Football
    today -- switch on by adding the key alone.
    """
    value = getattr(settings, name, "") or ""
    if not value:
        value = os.environ.get(name.upper(), "") or ""
    if not value:
        value = _dotenv_values().get(name.upper(), "")
    return value.strip() if isinstance(value, str) else ""


def provider_status() -> dict[str, bool]:
    """Which providers this deployment can actually reach."""
    return {
        "newsapi": bool(_key("newsapi_api_key")),
        "serper": bool(_key("serper_api_key")),
        "gnews": bool(_key("gnews_api_key")),
        "api_football": bool(_key("api_football_key")),
    }


# ── news providers ────────────────────────────────────────────────────────


async def _fetch_newsapi(
    client: httpx.AsyncClient, query: str, since: date, limit: int
) -> list[NewsItem]:
    api_key = _key("newsapi_api_key")
    if not api_key:
        return []
    # The free plan serves roughly the last month and answers 426 Upgrade
    # Required for anything older -- which cost every NewsAPI result whenever
    # the caller asked for a wider window. Clamping here keeps the request
    # inside what the plan allows instead of losing the provider entirely;
    # Serper still covers the rest of the window.
    earliest = max(since, date.today() - timedelta(days=_NEWSAPI_MAX_WINDOW_DAYS))
    response = await client.get(
        "https://newsapi.org/v2/everything",
        params={
            "q": query,
            "from": earliest.isoformat(),
            "sortBy": "publishedAt",
            "language": "en",
            "pageSize": max(1, min(limit, 100)),
        },
        headers={"X-Api-Key": api_key},
    )
    response.raise_for_status()
    body = response.json()
    if str(body.get("status")) != "ok":
        raise RuntimeError(str(body.get("message") or "NewsAPI rejected the query"))
    items = []
    for article in body.get("articles") or []:
        url = str(article.get("url") or "")
        items.append(
            NewsItem(
                title=str(article.get("title") or ""),
                url=url,
                domain=_domain_of(url),
                published_at=str(article.get("publishedAt") or ""),
                snippet=str(article.get("description") or ""),
                provider="newsapi",
            )
        )
    return items


async def _fetch_serper_news(
    client: httpx.AsyncClient, query: str, since: date, limit: int
) -> list[NewsItem]:
    api_key = _key("serper_api_key")
    if not api_key:
        return []
    response = await client.post(
        "https://google.serper.dev/news",
        json={"q": query, "num": max(1, min(limit, 100))},
        headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
    )
    response.raise_for_status()
    items = []
    for entry in response.json().get("news") or []:
        url = str(entry.get("link") or "")
        items.append(
            NewsItem(
                title=str(entry.get("title") or ""),
                url=url,
                domain=_domain_of(url),
                # Serper reports "2 days ago" rather than a date. Left as
                # given: a wrong precise date would be worse than a vague one,
                # and the recency filter treats undated items on their merits.
                published_at=str(entry.get("date") or ""),
                snippet=str(entry.get("snippet") or ""),
                provider="serper",
            )
        )
    return items


async def _fetch_gnews(
    client: httpx.AsyncClient, query: str, since: date, limit: int
) -> list[NewsItem]:
    api_key = _key("gnews_api_key")
    if not api_key:
        return []
    response = await client.get(
        "https://gnews.io/api/v4/search",
        params={
            "q": query,
            "from": f"{since.isoformat()}T00:00:00Z",
            "lang": "en",
            "max": max(1, min(limit, 100)),
            "apikey": api_key,
        },
    )
    response.raise_for_status()
    items = []
    for article in response.json().get("articles") or []:
        url = str(article.get("url") or "")
        items.append(
            NewsItem(
                title=str(article.get("title") or ""),
                url=url,
                domain=_domain_of(url),
                published_at=str(article.get("publishedAt") or ""),
                snippet=str(article.get("description") or ""),
                provider="gnews",
            )
        )
    return items


_NEWS_PROVIDERS = (
    ("newsapi", _fetch_newsapi),
    ("serper", _fetch_serper_news),
    ("gnews", _fetch_gnews),
)


async def fetch_current_news(
    query: str,
    *,
    trusted_domains: tuple[str, ...],
    days: int = DEFAULT_WINDOW_DAYS,
    limit: int = 12,
    today: date | None = None,
) -> list[NewsItem]:
    """Dated articles about *query* from every configured news provider.

    Providers run concurrently and are allowed to fail independently: one key
    being wrong costs its results, not the research. Results are restricted to
    the outlets already trusted for football reporting, deduplicated by URL,
    and returned newest first.

    Every item returned carries a date we could read and that falls inside the
    window. An article whose date cannot be parsed is rejected rather than
    assumed recent: these become "current reporting" downstream, and an
    unknown age asserted as current is the staleness this module exists to
    prevent.

    Every item is also actually about the topic. Trusted and recent is not
    enough -- a search for one player returns other players' transfers from
    the same outlets on the same days, and those were reaching the brief as
    facts about him.

    An empty list means nothing current was found, which is a fact the caller
    needs -- not a reason to fall back on memory.
    """
    text = " ".join(str(query or "").split())
    if not text:
        return []

    now = today or date.today()
    since = now - timedelta(days=max(1, days))
    terms = topic_terms(text)

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(_PROVIDER_TIMEOUT_SECONDS, connect=6.0),
        follow_redirects=True,
    ) as client:
        results = await asyncio.gather(
            *[fn(client, text, since, limit) for _, fn in _NEWS_PROVIDERS],
            return_exceptions=True,
        )

    merged: list[NewsItem] = []
    seen: set[str] = set()
    for (name, _), result in zip(_NEWS_PROVIDERS, results):
        if isinstance(result, BaseException):
            logger.warning(f"news provider {name} unavailable: {result}")
            continue
        for item in result:
            if not item.url or not item.title:
                continue
            if not _is_trusted(item.domain, trusted_domains):
                continue
            # A date we cannot read is not a date we can vouch for. These
            # results become "current reporting" in the brief, so an item of
            # unknown age would be asserted as current on no evidence -- and
            # a four-month-old article reached a thirty-day window exactly
            # that way, by being unparseable rather than by being recent.
            # Unknown is rejected, not assumed fresh.
            age = item.age_days(now)
            if age is None:
                logger.debug(
                    f"dropping {item.domain} item with unreadable date "
                    f"{item.published_at!r}: cannot confirm it is current"
                )
                continue
            if age > days:
                continue
            # Trusted and recent is not the same as relevant. A search for one
            # player returns squad round-ups and other players' transfers from
            # the same outlets on the same days, and those entered the brief
            # as facts about him.
            if not is_relevant(item, terms):
                logger.debug(
                    f"dropping off-topic {item.domain} item: {item.title[:70]!r}"
                )
                continue
            fingerprint = item.url.split("?", 1)[0].rstrip("/").lower()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            merged.append(item)

    # Newest first. Everything here carries a date we could read -- undated
    # items were rejected above rather than sorted to the bottom.
    merged.sort(
        key=lambda i: (_parse_date(i.published_at, today=now) or date.min),
        reverse=True,
    )
    return merged[:limit]


# ── structured squad data ─────────────────────────────────────────────────


async def fetch_player_facts(name: str, *, season: int | None = None) -> PlayerFacts | None:
    """Current club and squad record for a player, or None.

    A headline says a transfer is agreed; a squad record says where the player
    actually is. Where both exist the record wins, which is the whole reason
    for preferring a data provider over reporting for this one question.

    Returns None when no API-Football key is configured, so the caller falls
    back to dated reporting rather than to memory.
    """
    api_key = _key("api_football_key")
    player = " ".join(str(name or "").split())
    if not api_key or not player:
        return None

    year = season or (date.today().year if date.today().month >= 7 else date.today().year - 1)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(_PROVIDER_TIMEOUT_SECONDS, connect=6.0)
        ) as client:
            response = await client.get(
                "https://v3.football.api-sports.io/players",
                params={"search": player, "season": year},
                headers={"x-apisports-key": api_key},
            )
            response.raise_for_status()
            body = response.json()
    except Exception as exc:
        logger.warning(f"API-Football unavailable for {player!r}: {exc}")
        return None

    # The provider reports refusals in the body with HTTP 200. A free plan
    # only serves historical seasons, and a 2024 squad record is not an answer
    # to "where does this player play now" -- it is the stale answer this
    # module exists to avoid. Say why it is unavailable and let the caller
    # fall back to current reporting.
    errors = body.get("errors")
    if isinstance(errors, dict) and errors:
        logger.warning(
            f"API-Football returned no data for {player!r} "
            f"({'; '.join(f'{k}: {v}' for k, v in errors.items())}); "
            f"using dated reporting instead"
        )
        return None

    entries = body.get("response") or []
    if not entries:
        logger.info(f"API-Football has no squad record for {player!r}")
        return None

    first = entries[0]
    person = first.get("player") or {}
    stats = (first.get("statistics") or [{}])[0]
    team = (stats.get("team") or {}).get("name") or ""
    league = (stats.get("league") or {}).get("name") or ""

    return PlayerFacts(
        name=str(person.get("name") or player),
        team=str(team),
        league=str(league),
        season=str(year),
        nationality=str(person.get("nationality") or ""),
        age=person.get("age") if isinstance(person.get("age"), int) else None,
        position=str(stats.get("games", {}).get("position") or ""),
    )


def render_evidence(items: list[NewsItem], *, today: date | None = None) -> str:
    """The retrieved articles as a dated list a prompt can be held to."""
    if not items:
        return ""
    now = today or date.today()
    lines = ["CURRENT REPORTING (retrieved just now, newest first):"]
    for item in items:
        age = item.age_days(now)
        resolved = _parse_date(item.published_at, today=now)
        # The resolved date, not the raw string: providers write "1 month ago"
        # and slicing that to ten characters produced "1 month ag".
        when = resolved.isoformat() if resolved else (
            item.published_at or "date not given"
        )
        recency = f", {age}d ago" if age is not None else ""
        lines.append(f"- [{item.domain}, {when}{recency}] {item.title}")
        if item.snippet:
            lines.append(f"    {item.snippet[:220]}")
        lines.append(f"    {item.url}")
    return "\n".join(lines)


def render_player_facts(facts: PlayerFacts | None) -> str:
    """The squad record as a line the script must not contradict."""
    if not facts or not facts.team:
        return ""
    bits = [f"{facts.name} is registered with {facts.team}"]
    if facts.league:
        bits.append(f"in the {facts.league}")
    if facts.season:
        bits.append(f"for the {facts.season} season")
    return (
        "SQUAD RECORD (structured data, authoritative for current club):\n- "
        + " ".join(bits)
        + f" [source: {facts.provider}]"
    )
