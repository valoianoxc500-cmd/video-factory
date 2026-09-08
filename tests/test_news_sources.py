"""Current football facts come from dated reporting, not model memory.

The provider layer's job is narrow and its failures are quiet, which is why
these are worth pinning: a date it cannot parse becomes an undated item, an
undated item skips the recency filter, and a four-month-old article is then
handed to the scripter as current. A live check did exactly that before the
parser learned Google News' wording.
"""

from __future__ import annotations

import asyncio
from datetime import date

import pytest

from core import news_sources
from core.news_sources import NewsItem, fetch_current_news, render_evidence

TODAY = date(2026, 9, 7)
TRUSTED = ("bbc.com", "skysports.com", "transfermarkt.com", "bbc.co.uk")


def _item(**kwargs) -> NewsItem:
    base = {
        # Names the subject of the default query below, so the relevance
        # filter is not what these cases are exercising.
        "title": "Kylian Mbappe latest",
        "url": "https://bbc.com/sport/1",
        "domain": "bbc.com",
        "published_at": "2026-09-06T10:00:00Z",
        "snippet": "",
        "provider": "test",
    }
    base.update(kwargs)
    return NewsItem(**base)


# --- date parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-09-06T10:00:00Z", date(2026, 9, 6)),
        ("2026-09-06", date(2026, 9, 6)),
        ("2 hours ago", date(2026, 9, 7)),
        ("1 day ago", date(2026, 9, 6)),
        ("1 week ago", date(2026, 8, 31)),
        ("2 weeks ago", date(2026, 8, 24)),
        ("1 month ago", date(2026, 8, 8)),
        ("today", date(2026, 9, 7)),
        ("yesterday", date(2026, 9, 6)),
        ("May 6, 2026", date(2026, 5, 6)),
        ("6 May 2026", date(2026, 5, 6)),
    ],
)
def test_dates_google_news_actually_returns(raw, expected):
    assert news_sources._parse_date(raw, today=TODAY) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # The four Serper shapes that must work, spelled out.
        ("May 6, 2026", date(2026, 5, 6)),
        ("1 week ago", date(2026, 8, 31)),
        ("2 days ago", date(2026, 9, 5)),
        ("3 months ago", date(2026, 6, 8)),
        # Google News abbreviates September to "Sept", which %b rejects.
        ("Sept 6, 2026", date(2026, 9, 6)),
        ("Sept. 6, 2026", date(2026, 9, 6)),
    ],
)
def test_the_serper_formats_that_must_parse(raw, expected):
    assert news_sources._parse_date(raw, today=TODAY) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "not a date", "sometime last season", "last week", "recently"],
)
def test_an_unparseable_date_is_none_not_a_guess(raw):
    assert news_sources._parse_date(raw, today=TODAY) is None


# --- unknown is never fresh -----------------------------------------------


@pytest.mark.parametrize(
    "raw", ["", "unknown", "recently", "last season", "not a date"]
)
def test_an_item_with_an_unreadable_date_is_rejected(monkeypatch, raw):
    """Unknown age must not be asserted as current reporting.

    These items become the "CURRENT REPORTING" block the script is held to,
    so an article of unknown age would be presented as current on no evidence.
    """
    _stub_providers(
        monkeypatch,
        [
            _item(url="https://bbc.com/undated", published_at=raw),
            _item(url="https://bbc.com/dated", published_at="2 days ago"),
        ],
    )

    assert [i.url for i in _fetch(days=30)] == ["https://bbc.com/dated"]


def test_every_returned_item_has_a_readable_date(monkeypatch):
    _stub_providers(
        monkeypatch,
        [
            _item(url="https://bbc.com/a", published_at="3 months ago"),
            _item(url="https://bbc.com/b", published_at="unknown"),
            _item(url="https://bbc.com/c", published_at="May 6, 2026"),
            _item(url="https://bbc.com/d", published_at="2 days ago"),
        ],
    )

    got = _fetch(days=120)

    assert got, "the readable ones should survive"
    assert all(i.age_days(TODAY) is not None for i in got)
    assert "https://bbc.com/b" not in [i.url for i in got]


def test_all_undated_means_no_current_reporting(monkeypatch):
    """Better to report nothing found than to invent freshness."""
    _stub_providers(
        monkeypatch,
        [
            _item(url="https://bbc.com/1", published_at=""),
            _item(url="https://bbc.com/2", published_at="recently"),
        ],
    )

    assert _fetch(days=30) == []


# --- the staleness bug ----------------------------------------------------


def _stub_providers(monkeypatch, items: list[NewsItem]) -> None:
    async def one(client, query, since, limit):
        return list(items)

    async def none(client, query, since, limit):
        return []

    monkeypatch.setattr(
        news_sources, "_NEWS_PROVIDERS", (("stub", one), ("empty", none))
    )


def _fetch(**kwargs) -> list[NewsItem]:
    return asyncio.run(
        fetch_current_news(
            "kylian mbappe current club",
            trusted_domains=TRUSTED,
            today=TODAY,
            **kwargs,
        )
    )


def test_a_four_month_old_article_is_not_current(monkeypatch):
    """The exact miss: "May 6, 2026" read as undated and skipped the filter."""
    _stub_providers(
        monkeypatch,
        [
            _item(url="https://bbc.com/old", published_at="May 6, 2026"),
            _item(url="https://bbc.com/new", published_at="2 hours ago"),
        ],
    )

    got = _fetch(days=30)

    assert [i.url for i in got] == ["https://bbc.com/new"]


def test_relative_dates_inside_the_window_are_kept(monkeypatch):
    _stub_providers(
        monkeypatch,
        [
            _item(url="https://bbc.com/a", published_at="1 week ago"),
            _item(url="https://bbc.com/b", published_at="2 hours ago"),
        ],
    )

    assert len(_fetch(days=30)) == 2


def test_results_are_newest_first(monkeypatch):
    _stub_providers(
        monkeypatch,
        [
            _item(url="https://bbc.com/week", published_at="1 week ago"),
            _item(url="https://bbc.com/now", published_at="2 hours ago"),
            _item(url="https://bbc.com/day", published_at="1 day ago"),
        ],
    )

    assert [i.url.rsplit("/", 1)[-1] for i in _fetch(days=30)] == ["now", "day", "week"]


# --- relevance to the actual topic ----------------------------------------
#
# Live run of "Barcelona interest in Julian Alvarez": eight trusted, recent
# articles came back and three were about something else entirely. They
# matched the generic half of the query -- Arsenal, transfer -- and entered
# the brief as facts about Alvarez.

ALVAREZ = "Barcelona interest in Julian Alvarez"

# Verbatim from that run.
OFF_TOPIC = [
    (
        "Arsenal had more than 20 players on shortlist to capitalise on title win",
        "Senior football correspondent Sami Mokbel goes inside Arsenal's bid to "
        "capitalise on their Premier League title and strengthen their squad.",
    ),
    (
        "Gabriel Martinelli transfer grades: Arsenal attacker moves to Al-Hilal "
        "in $81 million deal",
        "The Gunners made quite a profit on the Brazilian, obliterating their "
        "previous record sale of Alex Oxlade-Chamberlain",
    ),
]

ON_TOPIC = [
    (
        "Barcelona, Atletico, acrimony and Arsenal: The Julian Alvarez transfer "
        "saga is not over",
        "The 26-year-old Argentina striker was Barcelona's main target of the "
        "summer transfer window, while Arsenal also expressed an interest.",
    ),
    (
        "Barcelona hopeful of signing Julián Álvarez from Atlético Madrid - "
        "Joan Laporta",
        "Barcelona president Joan Laporta says the club's offer stands.",
    ),
    (
        "Laporta: Barcelona remain 'very interested' in signing Atletico "
        "Madrid's Julian Alvarez",
        "",
    ),
    (
        "Julian Alvarez transfer news: Arsenal, Barcelona or Atletico Madrid "
        "stay for striker?",
        "",
    ),
    (
        "Julian Alvarez Receives Two Harsh Reality Checks After Summer "
        "Transfer Saga",
        "Barcelona star Dani Olmo claims he simply doesn't understand what's "
        "happening with Atletico Madrid.",
    ),
]


@pytest.mark.parametrize("title,snippet", OFF_TOPIC)
def test_an_article_that_never_mentions_the_player_is_rejected(title, snippet):
    terms = news_sources.topic_terms(ALVAREZ)
    assert news_sources.is_relevant(_item(title=title, snippet=snippet), terms) is False


@pytest.mark.parametrize("title,snippet", ON_TOPIC)
def test_an_article_about_the_player_is_kept(title, snippet):
    terms = news_sources.topic_terms(ALVAREZ)
    assert news_sources.is_relevant(_item(title=title, snippet=snippet), terms) is True


def test_accented_spellings_still_match():
    """ESPN writes "Julián Álvarez"; that piece names his current club."""
    terms = news_sources.topic_terms(ALVAREZ)
    item = _item(title="Barcelona hopeful of signing Julián Álvarez from Atlético Madrid")
    assert news_sources.is_relevant(item, terms) is True


def test_the_alvarez_query_keeps_only_the_on_topic_articles(monkeypatch):
    """The whole live result set, filtered."""
    _stub_providers(
        monkeypatch,
        [
            _item(url=f"https://bbc.com/off{i}", title=t, snippet=s, published_at="2 days ago")
            for i, (t, s) in enumerate(OFF_TOPIC)
        ]
        + [
            _item(url=f"https://bbc.com/on{i}", title=t, snippet=s, published_at="2 days ago")
            for i, (t, s) in enumerate(ON_TOPIC)
        ],
    )

    got = asyncio.run(
        fetch_current_news(
            ALVAREZ, trusted_domains=TRUSTED, today=TODAY, days=30, limit=20
        )
    )

    assert len(got) == len(ON_TOPIC)
    assert all("alvarez" in news_sources._normalise(i.title) for i in got)


def test_generic_football_words_are_not_topic_terms():
    """Otherwise every transfer story matches every other one."""
    assert news_sources.topic_terms("transfer news latest club deal rumours") == []


def test_topic_terms_keep_the_names():
    assert set(news_sources.topic_terms(ALVAREZ)) == {"barcelona", "julian", "alvarez"}


def test_a_topic_with_no_usable_terms_filters_nothing():
    """An unfiltered brief is a smaller problem than an empty one."""
    assert news_sources.is_relevant(_item(title="Anything at all"), []) is True


def test_a_surname_only_headline_still_counts():
    """"Rashford's return..." is about Rashford even without the first name."""
    terms = news_sources.topic_terms("Marcus Rashford transfer")
    item = _item(title="Rashford's return to Man United brings a lot of questions")
    assert news_sources.is_relevant(item, terms) is True


def test_a_club_only_headline_does_not_carry_a_player_topic():
    terms = news_sources.topic_terms("Marcus Rashford transfer")
    item = _item(title="Man Utd see positives despite continuing to drop points")
    assert news_sources.is_relevant(item, terms) is False


# --- filtering and merging ------------------------------------------------


def test_untrusted_outlets_are_dropped(monkeypatch):
    """A run was discarded for citing YouTube, Facebook and Reddit."""
    _stub_providers(
        monkeypatch,
        [
            _item(url="https://youtube.com/watch?v=1", domain="youtube.com"),
            _item(url="https://reddit.com/r/soccer/1", domain="reddit.com"),
            _item(url="https://bbc.com/sport/1", domain="bbc.com"),
        ],
    )

    assert [i.domain for i in _fetch()] == ["bbc.com"]


def test_a_subdomain_of_a_trusted_outlet_counts(monkeypatch):
    _stub_providers(
        monkeypatch, [_item(url="https://sport.bbc.com/1", domain="sport.bbc.com")]
    )
    assert len(_fetch()) == 1


def test_the_same_story_from_two_providers_appears_once(monkeypatch):
    _stub_providers(
        monkeypatch,
        [
            _item(url="https://bbc.com/sport/1", provider="newsapi"),
            _item(url="https://bbc.com/sport/1?utm_source=x", provider="serper"),
            _item(url="https://bbc.com/sport/1/", provider="gnews"),
        ],
    )

    assert len(_fetch()) == 1


def test_one_provider_failing_does_not_lose_the_others(monkeypatch):
    async def broken(client, query, since, limit):
        raise RuntimeError("401 unauthorised")

    async def working(client, query, since, limit):
        return [_item(url="https://skysports.com/1", domain="skysports.com")]

    monkeypatch.setattr(
        news_sources, "_NEWS_PROVIDERS", (("broken", broken), ("working", working))
    )

    assert [i.domain for i in _fetch()] == ["skysports.com"]


def test_no_query_means_no_request(monkeypatch):
    async def explode(client, query, since, limit):
        raise AssertionError("should not have been called")

    monkeypatch.setattr(news_sources, "_NEWS_PROVIDERS", (("x", explode),))
    assert asyncio.run(
        fetch_current_news("   ", trusted_domains=TRUSTED, today=TODAY)
    ) == []


# --- providers switch on their own keys -----------------------------------


def test_a_provider_without_a_key_returns_nothing(monkeypatch):
    monkeypatch.setattr(news_sources, "_key", lambda name: "")

    async def run() -> list:
        return await news_sources._fetch_newsapi(None, "q", TODAY, 5)

    assert asyncio.run(run()) == []


def test_player_facts_are_none_without_api_football(monkeypatch):
    """No key must mean "unavailable", never a guess."""
    monkeypatch.setattr(news_sources, "_key", lambda name: "")
    assert asyncio.run(news_sources.fetch_player_facts("Marcus Rashford")) is None


def test_a_plan_refusal_is_unavailable_not_stale_data(monkeypatch):
    """A free API-Football plan serves 2022-2024 only, with HTTP 200.

    Returning a 2024 squad record for "where does this player play now" is the
    exact staleness this module exists to prevent, so a refusal must read as
    no answer rather than an old one.
    """

    class _Response:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "errors": {
                    "plan": "Free plans do not have access to this season, "
                            "try from 2022 to 2024."
                },
                "results": 0,
                "response": [],
            }

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, *a, **k):
            return _Response()

    monkeypatch.setattr(news_sources, "_key", lambda name: "a-key")
    monkeypatch.setattr(news_sources.httpx, "AsyncClient", lambda **k: _Client())

    assert asyncio.run(news_sources.fetch_player_facts("Marcus Rashford")) is None


def test_a_key_only_in_dotenv_is_still_found(monkeypatch, tmp_path):
    """Settings ignores fields it has no attribute for.

    API_FOOTBALL_KEY was in .env and invisible to the app: pydantic-settings
    reads the file but drops unknown keys, and it never reaches os.environ
    either, so both earlier lookups missed it.
    """
    env = tmp_path / ".env"
    env.write_text(
        '# a comment\nAPI_FOOTBALL_KEY="secret-value"\nOTHER=1\n', encoding="utf-8"
    )
    monkeypatch.setattr(news_sources, "_dotenv_path", lambda: env)
    monkeypatch.delenv("API_FOOTBALL_KEY", raising=False)
    news_sources._dotenv_values.cache_clear()

    try:
        assert news_sources._key("api_football_key") == "secret-value"
        assert news_sources.provider_status()["api_football"] is True
    finally:
        news_sources._dotenv_values.cache_clear()


def test_a_real_environment_variable_beats_dotenv(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    env.write_text("API_FOOTBALL_KEY=from-file\n", encoding="utf-8")
    monkeypatch.setattr(news_sources, "_dotenv_path", lambda: env)
    monkeypatch.setenv("API_FOOTBALL_KEY", "from-environment")
    news_sources._dotenv_values.cache_clear()

    try:
        assert news_sources._key("api_football_key") == "from-environment"
    finally:
        news_sources._dotenv_values.cache_clear()


def test_provider_status_reports_what_is_configured(monkeypatch):
    monkeypatch.setattr(
        news_sources, "_key", lambda name: "k" if name == "newsapi_api_key" else ""
    )
    status = news_sources.provider_status()
    assert status["newsapi"] is True
    assert status["gnews"] is False and status["api_football"] is False


# --- what the prompt is given ---------------------------------------------


def test_evidence_carries_the_date_domain_and_link():
    text = render_evidence(
        [_item(title="Rashford loan explored", published_at="2026-09-06T09:00:00Z")],
        today=TODAY,
    )
    assert "Rashford loan explored" in text
    assert "bbc.com" in text
    assert "2026-09-06" in text
    assert "https://bbc.com/sport/1" in text
    assert "1d ago" in text


def test_a_relative_date_is_rendered_as_a_real_date():
    """Slicing the raw string to ten characters produced "1 month ag"."""
    text = render_evidence([_item(published_at="1 week ago")], today=TODAY)
    assert "2026-08-31" in text
    assert "1 month ag" not in text and "1 week ag]" not in text


def test_newsapi_window_is_clamped_to_what_the_plan_allows(monkeypatch):
    """Asking for 45 days got a 426 and lost the provider entirely."""
    seen: dict = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"status": "ok", "articles": []}

    class _Client:
        async def get(self, url, params=None, headers=None):
            seen.update(params or {})
            return _Response()

    monkeypatch.setattr(news_sources, "_key", lambda name: "a-key")
    asyncio.run(
        news_sources._fetch_newsapi(_Client(), "rashford", date(2026, 7, 24), 12)
    )

    asked_from = date.fromisoformat(seen["from"])
    assert (date.today() - asked_from).days <= news_sources._NEWSAPI_MAX_WINDOW_DAYS


def test_no_evidence_renders_nothing():
    assert render_evidence([], today=TODAY) == ""
