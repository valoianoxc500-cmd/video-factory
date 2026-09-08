"""Football research is grounded in retrieved reporting, and Horror is not.

Grounded model search already refuses to answer without citations. What it
could not do was guarantee anything worth citing came back: one run had its
research discarded because the model's own search returned YouTube, Facebook
and Reddit, and the scripter then wrote from an empty brief -- training data by
another route, on exactly the questions (which club, which transfer) that
training data gets wrong.

These cover the two halves of the fix: the discard paths now fall back to
articles retrieved from news APIs, and claims the reporting does not support
are marked down rather than narrated as fact.
"""

from __future__ import annotations

from core import researcher
from core.news_sources import NewsItem
from core.utils import load_channel_config


def _article(title: str, *, domain: str = "bbc.com", url: str = "") -> NewsItem:
    return NewsItem(
        title=title,
        url=url or f"https://{domain}/{abs(hash(title)) % 9999}",
        domain=domain,
        published_at="2026-09-06T09:00:00Z",
        snippet="",
        provider="test",
    )


# --- the gate: football only ----------------------------------------------


def test_football_is_a_news_channel():
    assert researcher._is_news_channel(load_channel_config("football_news")) is True


def test_horror_is_not_touched_by_any_of_this():
    """The whole feature must be invisible to the storytelling channel."""
    assert researcher._is_news_channel(load_channel_config("horror_stories")) is False


# --- the discard paths no longer hand back an empty brief -----------------


def test_discarded_research_falls_back_to_retrieved_reporting():
    evidence = [
        _article("Rashford loan to Arsenal explored", domain="skysports.com"),
        _article("Man Utd see positives despite dropped points"),
    ]

    out = researcher._research_from_evidence(evidence, reason="no search citations")

    assert out["brief"], "an empty brief is what sent the scripter back to memory"
    assert "Rashford loan to Arsenal explored" in out["brief"]
    assert out["evidence_only"] is True
    assert len(out["sources"]) == 2


def test_the_fallback_states_nothing_more_strongly_than_reported():
    """These are headlines. Nothing here has been cross-checked into a fact."""
    out = researcher._research_from_evidence(
        [_article("Club agree fee for player")], reason="whatever"
    )
    assert all(f["status"] == "REPORTED" for f in out["verified_facts"])


def test_with_nothing_retrieved_the_brief_is_honestly_empty():
    """No reporting found must not become an invented brief."""
    out = researcher._research_from_evidence([], reason="no search citations")
    assert out["brief"] == ""
    assert out["grounded"] is False
    assert out["rejected_reason"] == "no search citations"


# --- claim verification ---------------------------------------------------


def test_a_claim_no_article_supports_is_marked_down():
    research = {
        "verified_facts": [
            {"claim": "Kylian Mbappe has signed for Liverpool", "status": "COMPLETED"}
        ]
    }
    evidence = [_article("Marcus Rashford loan to Arsenal explored")]

    researcher._verify_claims_against_evidence(research, evidence)

    fact = research["verified_facts"][0]
    assert fact["status"] == "REPORTED"
    assert fact["unsupported_by_retrieved_reporting"] is True
    assert research["claims_downgraded"] == 1


def test_a_claim_the_reporting_backs_keeps_its_status():
    research = {
        "verified_facts": [
            {"claim": "Rashford loan to Arsenal explored", "status": "COMPLETED"}
        ]
    }
    evidence = [_article("Arsenal transfer news: Marcus Rashford loan could be explored")]

    researcher._verify_claims_against_evidence(research, evidence)

    assert research["verified_facts"][0]["status"] == "COMPLETED"
    assert "claims_downgraded" not in research


def test_verification_never_promotes_a_weaker_claim():
    """Agreeing with a headline is not verification."""
    research = {
        "verified_facts": [
            {"claim": "Rashford loan to Arsenal explored", "status": "RUMORED"}
        ]
    }
    evidence = [_article("Arsenal transfer news: Marcus Rashford loan could be explored")]

    researcher._verify_claims_against_evidence(research, evidence)

    assert research["verified_facts"][0]["status"] == "RUMORED"


def test_with_no_reporting_claims_are_left_alone():
    """Nothing to check against is not evidence of being wrong."""
    research = {
        "verified_facts": [{"claim": "Something happened", "status": "COMPLETED"}]
    }
    researcher._verify_claims_against_evidence(research, [])
    assert research["verified_facts"][0]["status"] == "COMPLETED"


def test_verification_survives_a_malformed_fact_list():
    research = {"verified_facts": ["not a dict", {"status": "COMPLETED"}, None]}
    researcher._verify_claims_against_evidence(research, [_article("Anything")])
    assert research["verified_facts"][0] == "not a dict"


# --- the tokeniser behind it ----------------------------------------------


def test_common_football_words_do_not_make_two_claims_match():
    """Otherwise every transfer story "supports" every other one."""
    tokens = researcher._claim_tokens("The club agreed a transfer deal for the player")
    assert tokens == set(), f"nothing distinctive should survive: {tokens}"


def test_names_and_clubs_survive_tokenising():
    tokens = researcher._claim_tokens("Rashford joins Arsenal on loan")
    assert "rashford" in tokens and "arsenal" in tokens
