"""Tests for match identification and the verified event timeline."""

import pytest

from core.match_data import (
    MatchEvent,
    MatchFacts,
    check_consistency,
    facts_from_payload,
    normalise_events,
    parse_fixture_query,
    research_brief,
    select_provider,
)


# --- fixture parsing -------------------------------------------------------

@pytest.mark.parametrize(
    "query,expected",
    [
        ("Monaco vs PSG", ("Monaco", "PSG")),
        ("Monaco VS PSG", ("Monaco", "PSG")),
        ("Monaco vs. PSG", ("Monaco", "PSG")),
        ("Monaco v PSG", ("Monaco", "PSG")),
        ("Monaco versus PSG", ("Monaco", "PSG")),
        ("Real Madrid ضد برشلونة", ("Real Madrid", "برشلونة")),
        ("Monaco - PSG", ("Monaco", "PSG")),
        ("  Monaco   vs   PSG  ", ("Monaco", "PSG")),
    ],
)
def test_fixture_query_splits_the_two_sides(query, expected):
    assert parse_fixture_query(query) == expected


@pytest.mark.parametrize("query", ["", "   ", "Monaco", "a vs b vs c"])
def test_unparseable_fixture_returns_empty(query):
    assert parse_fixture_query(query) == ("", "")


# --- provider selection ----------------------------------------------------

def test_structured_provider_wins_when_its_key_is_present(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "k")
    assert select_provider() == "structured"


def test_grounded_is_the_default_without_a_sports_feed(monkeypatch):
    monkeypatch.delenv("FOOTBALL_DATA_API_KEY", raising=False)
    assert select_provider() == "grounded"


def test_explicit_provider_overrides_detection(monkeypatch):
    monkeypatch.setenv("FOOTBALL_DATA_API_KEY", "k")
    assert select_provider("grounded") == "grounded"


# --- event normalisation ---------------------------------------------------

def test_events_are_sorted_and_coerced():
    events = normalise_events([
        {"type": "Red Card", "minute": "80", "player": "B"},
        {"type": "goal", "minute": 23, "player": "A", "confidence": "verified"},
    ])
    assert [e.minute for e in events] == [23, 80]
    assert events[1].type == "red_card"
    assert events[0].confidence == "verified"


def test_unusable_events_are_dropped_not_guessed():
    events = normalise_events([
        {"type": "goal"},                      # no minute
        {"minute": 10},                        # no type
        {"type": "goal", "minute": "later"},   # unparseable minute
        {"type": "goal", "minute": 400},       # impossible minute
        "not a dict",
        {"type": "goal", "minute": 45},        # the only good one
    ])
    assert len(events) == 1
    assert events[0].minute == 45


def test_unknown_confidence_degrades_to_unverified():
    events = normalise_events([{"type": "goal", "minute": 5, "confidence": "certain"}])
    assert events[0].confidence == "unverified"


# --- consistency -----------------------------------------------------------

def test_goal_count_matching_the_scoreline_is_clean():
    facts = MatchFacts(
        home_team="Monaco", away_team="PSG", home_score=1, away_score=2,
        events=[
            MatchEvent("goal", 12), MatchEvent("goal", 40), MatchEvent("goal", 77),
        ],
    )
    assert check_consistency(facts) == []


def test_goal_count_disagreeing_with_the_scoreline_is_flagged():
    facts = MatchFacts(
        home_team="Monaco", away_team="PSG", home_score=3, away_score=2,
        events=[MatchEvent("goal", 12)],
    )
    problems = check_consistency(facts)
    assert problems and "5 expected" in problems[0]


def test_missing_score_is_flagged():
    assert check_consistency(MatchFacts(home_team="A", away_team="B")) == [
        "final score unknown"
    ]


# --- payload -> facts ------------------------------------------------------

def _payload(**over):
    base = {
        "home_team": "Monaco", "away_team": "PSG",
        "home_score": 1, "away_score": 2, "competition": "Ligue 1",
        "played_on": "2026-09-01",
        "events": [
            {"type": "goal", "minute": 12, "player": "X", "confidence": "verified"},
            {"type": "goal", "minute": 40, "player": "Y", "confidence": "verified"},
            {"type": "goal", "minute": 77, "player": "Z", "confidence": "reported"},
        ],
    }
    base.update(over)
    return base


def test_identified_match_produces_a_scoreline_and_label():
    facts = facts_from_payload(_payload(), provider="structured")
    assert facts.identified is True
    assert facts.scoreline == "1-2"
    assert facts.label == "Monaco 1-2 PSG"
    assert facts.note == ""


def test_match_without_a_score_is_not_identified():
    facts = facts_from_payload(
        _payload(home_score=None, away_score=None), provider="grounded"
    )
    assert facts.identified is False


def test_moments_are_capped_and_ordered():
    events = [{"type": "goal", "minute": m} for m in range(1, 12)]
    facts = facts_from_payload(_payload(events=events), provider="structured")
    moments = facts.to_moments()
    assert len(moments) == 6                       # capped
    assert [m.minute for m in moments] == sorted(m.minute for m in moments)
    # No footage offsets until real footage is aligned to the match clock.
    assert all(m.footage_offset_seconds is None for m in moments)


def test_non_major_events_do_not_become_moments():
    facts = facts_from_payload(
        _payload(events=[
            {"type": "goal", "minute": 10},
            {"type": "substitution", "minute": 60},
            {"type": "corner", "minute": 70},
        ]),
        provider="structured",
    )
    assert [m.label for m in facts.to_moments()] == ["goal"]


# --- research brief --------------------------------------------------------

def test_brief_tags_claims_by_confidence():
    brief = research_brief(facts_from_payload(_payload(), provider="structured"))
    assert "FINAL SCORE [VERIFIED]: 1-2" in brief
    assert "[VERIFIED]" in brief
    assert "[REPORTED]" in brief


def test_brief_forbids_invention_when_the_match_is_unknown():
    facts = facts_from_payload({"home_team": "", "away_team": ""}, provider="grounded")
    brief = research_brief(facts)
    assert "MATCH NOT IDENTIFIED" in brief
    assert "Do not invent" in brief


def test_brief_warns_when_events_contradict_the_score():
    facts = facts_from_payload(
        _payload(events=[{"type": "goal", "minute": 12}]), provider="grounded"
    )
    brief = research_brief(facts)
    assert "DATA WARNING" in brief
    assert "not present the event list as a complete account" in brief
