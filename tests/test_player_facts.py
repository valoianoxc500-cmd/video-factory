"""A named player's current club comes from a record, not from memory.

Julián Álvarez left Manchester City for Atlético Madrid in August 2024. A
model writing from training data still puts him at City, and a transfer story
built on the wrong club is wrong from its first line. These pin the resolution
and, more importantly, the gate that throws out a claim naming a club he left.

The live provider confirms the shape these fixtures use:

    /transfers?player=6009  ->  2024-08-12  in Atletico Madrid  out Manchester City
                                2022-07-08  in Manchester City  out River Plate
    /players/squads         ->  Atletico Madrid (#19)
"""

from __future__ import annotations

import asyncio

import pytest

from core import player_facts
from core.player_facts import (
    PlayerStatus,
    cross_check,
    outdated_claim_reason,
    render_current_facts,
)

ALVAREZ = PlayerStatus(
    name="Julián Álvarez",
    player_id=6009,
    current_club="Atletico Madrid",
    former_clubs=["Manchester City", "River Plate"],
    source="api-football",
    last_transfer_date="2024-08-12",
)


class _Item:
    def __init__(self, title: str, snippet: str = ""):
        self.title = title
        self.snippet = snippet


# Verbatim headlines from the live run.
EVIDENCE = [
    _Item("Barcelona hopeful of signing Julián Álvarez from Atlético Madrid - Joan Laporta"),
    _Item("Laporta: Barcelona remain 'very interested' in signing Atletico Madrid's Julian Alvarez"),
    _Item("Julian Alvarez transfer news: Arsenal, Barcelona or Atletico Madrid stay for striker?"),
]

CLUBS = [
    "Barcelona", "Atletico Madrid", "Atlético Madrid", "Manchester City",
    "Arsenal", "River Plate",
]


# --- the expected answer --------------------------------------------------


def test_current_club_is_atletico_madrid():
    assert ALVAREZ.is_current("Atletico Madrid") is True
    assert ALVAREZ.current_club == "Atletico Madrid"


def test_manchester_city_is_history_not_current():
    assert ALVAREZ.is_current("Manchester City") is False
    assert ALVAREZ.is_former("Manchester City") is True


# --- the gate: a former club written as current ---------------------------


@pytest.mark.parametrize(
    "claim",
    [
        "Manchester City's Julian Alvarez is wanted by Barcelona",
        "Julian Alvarez of Manchester City has been linked with a move",
        "Manchester City striker Julian Alvarez is a target",
        "Alvarez remains at Manchester City despite the interest",
        "Barcelona want to sign Julian Alvarez from Manchester City",
    ],
)
def test_a_former_club_written_as_current_is_rejected(claim):
    reason = outdated_claim_reason(claim, ALVAREZ)
    assert reason, f"should have been rejected: {claim}"
    assert "Manchester City" in reason
    assert "Atletico Madrid" in reason


@pytest.mark.parametrize(
    "claim",
    [
        "Julian Alvarez left Manchester City in 2024",
        "Former Manchester City forward Julian Alvarez is settled in Madrid",
        "Alvarez joined Atletico Madrid from Manchester City",
        "Alvarez previously played for Manchester City",
        "Alvarez won the treble with Manchester City before his move",
    ],
)
def test_history_stated_as_history_is_allowed(claim):
    """Man City may appear -- as former-club history. That is the story."""
    assert outdated_claim_reason(claim, ALVAREZ) == ""


@pytest.mark.parametrize(
    "claim",
    [
        "Atletico Madrid's Julian Alvarez is a target for Barcelona",
        "Barcelona remain interested in Julian Alvarez",
        "Julian Alvarez of Atletico Madrid has scored again",
        "Barcelona hopeful of signing Julian Alvarez from Atletico Madrid",
    ],
)
def test_correct_and_interest_claims_pass(claim):
    assert outdated_claim_reason(claim, ALVAREZ) == ""


def test_a_claim_about_someone_else_is_not_judged():
    assert outdated_claim_reason(
        "Manchester City's Erling Haaland scored twice", ALVAREZ
    ) == ""


def test_an_unresolved_player_gates_nothing():
    """No record must mean no opinion, not a wrong one."""
    unknown = PlayerStatus(name="Someone Unknown")
    assert unknown.resolved is False
    assert outdated_claim_reason("Manchester City's Someone Unknown", unknown) == ""


# --- cross-checking reporting --------------------------------------------


def test_interest_is_read_from_reporting_but_never_as_the_current_club():
    status = PlayerStatus(
        name="Julián Álvarez",
        current_club="Atletico Madrid",
        former_clubs=["Manchester City"],
    )

    result = cross_check(status, EVIDENCE, CLUBS)

    assert "Barcelona" in result["interested"]
    assert status.current_club == "Atletico Madrid", "interest must not move a player"
    assert "Barcelona" not in status.current_club


def test_reporting_confirms_the_record():
    status = PlayerStatus(
        name="Julián Álvarez", current_club="Atletico Madrid", former_clubs=[]
    )
    assert cross_check(status, EVIDENCE, CLUBS)["confirms_current"] is True


def test_reporting_never_overrules_the_record():
    """A rumour that a move is agreed does not move a player."""
    status = PlayerStatus(
        name="Julián Álvarez",
        current_club="Atletico Madrid",
        former_clubs=["Manchester City"],
    )
    stale = [_Item("Manchester City's Julian Alvarez in fine form")]

    result = cross_check(status, stale, CLUBS)

    assert status.current_club == "Atletico Madrid"
    assert "Manchester City" in result["contradictions"]


# --- what the script and the image briefs are given -----------------------


def test_the_facts_block_states_current_and_former_clearly():
    text = render_current_facts([ALVAREZ])
    assert "Atletico Madrid" in text
    assert "Manchester City" in text
    assert "never write these as his current club" in text
    assert "2024-08-12" in text


def test_an_unresolved_player_contributes_nothing():
    assert render_current_facts([PlayerStatus(name="Nobody")]) == ""


# --- club spelling ---------------------------------------------------------


def test_provider_spelling_variants_are_the_same_club():
    """The provider says "Manchester United" and "Manchester Utd" for one club.

    Left unmatched, a player's current club also lands in his former list and
    a correct claim gets rejected as out of date.
    """
    rashford = PlayerStatus(
        name="Marcus Rashford",
        current_club="Manchester United",
        former_clubs=["Manchester Utd", "Aston Villa"],
    )

    assert rashford.is_current("Manchester Utd") is True
    assert rashford.is_former("Manchester Utd") is False
    assert outdated_claim_reason("Manchester Utd's Marcus Rashford scored", rashford) == ""


def test_fc_and_accents_do_not_change_the_club():
    status = PlayerStatus(name="X Y", current_club="Atlético Madrid")
    assert status.is_current("Atletico Madrid FC") is True


# --- no key, no guess ------------------------------------------------------


# --- step 5: the same facts reach the image briefs -------------------------

STATUS_ROWS = [
    {
        "name": "Julián Álvarez",
        "current_club": "Atletico Madrid",
        "former_clubs": ["Manchester City", "River Plate"],
        "interested_clubs": ["Barcelona"],
        "source": "api-football",
    }
]


def _script_with(keywords: str, prompt: str, visual: str = "google_photo") -> dict:
    return {
        "sections": [
            {
                "id": 1,
                "narration": "n",
                "slots": [{"visual": visual, "keywords": keywords, "prompt": prompt}],
            }
        ]
    }


def test_an_image_brief_naming_a_former_club_is_rewritten():
    """A real photo of the right player in the wrong shirt passes the gate."""
    from core.scripter import repair_outdated_club_briefs

    script = _script_with(
        "Julian Alvarez Manchester City kit photograph",
        "Julián Álvarez wearing the Manchester City home kit during a match.",
    )

    assert repair_outdated_club_briefs(script, STATUS_ROWS) == 2

    slot = script["sections"][0]["slots"][0]
    assert "Manchester City" not in slot["keywords"]
    assert "Manchester City" not in slot["prompt"]
    assert "Atletico Madrid" in slot["keywords"]
    assert "Atletico Madrid" in slot["prompt"]


def test_a_brief_already_naming_the_current_club_is_untouched():
    from core.scripter import repair_outdated_club_briefs

    script = _script_with(
        "Julian Alvarez Atletico Madrid kit photograph",
        "Julián Álvarez wearing the Atlético Madrid home kit.",
    )
    before = dict(script["sections"][0]["slots"][0])

    assert repair_outdated_club_briefs(script, STATUS_ROWS) == 0
    assert script["sections"][0]["slots"][0] == before


def test_a_club_brief_not_about_the_player_is_untouched():
    """"Manchester City stadium" is not a claim about where Álvarez plays."""
    from core.scripter import repair_outdated_club_briefs

    script = _script_with(
        "Manchester City stadium exterior photograph",
        "The Etihad Stadium seen from outside on a matchday.",
    )

    assert repair_outdated_club_briefs(script, STATUS_ROWS) == 0
    assert "Manchester City" in script["sections"][0]["slots"][0]["keywords"]


def test_no_resolved_players_means_no_rewriting():
    from core.scripter import repair_outdated_club_briefs

    script = _script_with("Julian Alvarez Manchester City", "Álvarez at City.")
    assert repair_outdated_club_briefs(script, []) == 0


def test_component_slots_are_not_rewritten():
    from core.scripter import repair_outdated_club_briefs

    script = _script_with(
        "Julian Alvarez Manchester City", "Álvarez at City.", visual="bar_chart"
    )
    assert repair_outdated_club_briefs(script, STATUS_ROWS) == 0


def test_no_api_key_resolves_to_none(monkeypatch):
    monkeypatch.setattr(player_facts, "_key", lambda name: "")
    assert asyncio.run(player_facts.resolve_player("Julian Alvarez")) is None


def test_an_empty_name_resolves_to_none(monkeypatch):
    monkeypatch.setattr(player_facts, "_key", lambda name: "a-key")
    assert asyncio.run(player_facts.resolve_player("   ")) is None
