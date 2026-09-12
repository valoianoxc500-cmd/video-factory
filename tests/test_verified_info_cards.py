"""The safe floor under image sourcing, and the recovery contract around it.

All of this comes from one real run, 99db2deb-4a16-4467-b318-a01fba131b7e
("Real Madrid Vs Inter Milan recent game"). Research grounded it to a cited
ESPN report -- Real Madrid 2-1 Inter Milan, 8 September 2026, Champions League
-- and the run then wrote a script, recorded narration, and threw all of it
away because nine beats could not obtain a photograph the relevance gate would
accept. Fourteen beats that *had* passed were discarded with them.

What is tested here is the floor: those nine beats become editorial cards built
only from the citations, the fourteen keep their photographs, and the stage
completes. What is deliberately also tested is everything the floor must refuse
-- an invented scoreline, an uncited player, a card that talks like a
photograph, and a run with no citations at all, which must stay terminal.
"""

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
# worker.py imports its siblings by bare name, the way it is launched in
# production; every other worker test does the same.
sys.path.insert(0, str(REPO_ROOT / "worker"))

from core import reliability, verified_cards
from core.reliability import FailureKind, RunAttempt
from core.utils import ChannelConfig, Script, ScriptSection, VisualSlot

# The real brief, trimmed to the lines that carry facts.
RESEARCH = {
    "brief": (
        "VERIFIED AGAINST CURRENT REPORTING ONLY.\n"
        "- [espn.com, 2026-09-08, 3d ago] Real Madrid 2-1 Inter Milan "
        "(Sep 8, 2026) Game Analysis\n"
        "    Expert recap and game analysis of the Real Madrid vs. "
        "Internazionale Uefa Champions League game from September 8, 2026 "
        "on ESPN."
    ),
    "sources": [{
        "title": "Real Madrid 2-1 Inter Milan (Sep 8, 2026) Final Score",
        "url": "https://www.espn.com/soccer/report/_/gameId/401915451",
        "domain": "espn.com",
    }],
    "verified_facts": [{
        "claim": "Real Madrid 2-1 Inter Milan (Sep 8, 2026) Game Analysis",
        "status": "REPORTED",
        "source": "espn.com",
    }],
    "grounded": True,
}

BRIEF_LINE = (
    "- [espn.com, 2026-09-08, 3d ago] Real Madrid 2-1 Inter Milan "
    "(Sep 8, 2026) Game Analysis"
)


def _workspace(tmp_path, research=RESEARCH):
    if research is not None:
        (tmp_path / "research.json").write_text(
            json.dumps(research, ensure_ascii=False), encoding="utf-8"
        )
    return tmp_path


@pytest.fixture
def facts(tmp_path):
    return verified_cards.load_grounded_facts(_workspace(tmp_path))


def _config(**overrides) -> ChannelConfig:
    cfg = ChannelConfig.model_validate(json.loads(
        (__import__("pathlib").Path("config/channels/football_news.json"))
        .read_text(encoding="utf-8")
    ))
    for key, value in overrides.items():
        setattr(cfg.image_sourcing, key, value)
    return cfg


def _script(slot_count: int = 3) -> Script:
    return Script(
        title="ريال مدريد ضد إنتر ميلان",
        video_type="news",
        sections=[ScriptSection(
            id=1,
            narration="لم تكن مجرد مباراة عادية بل كانت معركة تكتيكية شرسة.",
            slots=[
                VisualSlot(visual="google_photo", prompt=f"beat {i}", keywords="k")
                for i in range(1, slot_count + 1)
            ],
        )],
    )


# ── the card itself ──────────────────────────────────────────────────

def test_the_run_is_grounded_from_its_own_research_file(facts):
    assert facts is not None
    assert (facts.home, facts.home_score, facts.away_score, facts.away) == (
        "Real Madrid", "2", "1", "Inter Milan"
    )
    assert facts.date_text == "Sep 8, 2026"
    assert facts.competition_key == "uefa champions league"
    assert facts.sources == ("espn.com",)


def test_an_ungrounded_run_yields_no_facts(tmp_path):
    """No citations means no card. This is the terminal case, not a gap."""
    ungrounded = dict(RESEARCH, grounded=False)
    assert verified_cards.load_grounded_facts(_workspace(tmp_path, ungrounded)) is None
    assert verified_cards.load_grounded_facts(tmp_path / "nowhere") is None


@pytest.mark.parametrize("language", ["ar", "en"])
def test_the_card_states_the_cited_result(facts, language):
    props = verified_cards.build_card_props(facts, language)
    text = props["text"]
    assert "Real Madrid" in text and "Inter Milan" in text
    assert "2" in text and "1" in text
    assert "Sep 8, 2026" in text
    assert "espn.com" in text
    assert props[verified_cards.VERIFIED_CARD_KEY] is True
    assert verified_cards.verify_card_text(text, facts, language) == []


def test_the_card_is_labelled_a_graphic_not_a_photograph(facts):
    """It must announce what it is, and must not carry an illustration."""
    props = verified_cards.build_card_props(facts, "ar")
    assert "معلومات موثقة" in props["text"]
    assert "illustration_url" not in props
    assert props["card_kind"] == "verified_information"


def test_an_unlabelled_card_is_refused(facts):
    problems = verified_cards.verify_card_text(
        "Real Madrid  2 – 1  Inter Milan", facts, "en"
    )
    assert any("verified information" in p for p in problems)


def test_a_fabricated_scoreline_cannot_enter_a_card(facts):
    """Per-digit checking is not enough: '3' occurs in '3d ago'."""
    problems = verified_cards.verify_card_text(
        "VERIFIED INFORMATION\nReal Madrid  3 – 1  Inter Milan", facts, "en"
    )
    assert any("does not match the cited 2-1" in p for p in problems)


def test_the_cited_result_cannot_be_given_to_the_wrong_side(facts):
    problems = verified_cards.verify_card_text(
        "VERIFIED INFORMATION\nInter Milan  2 – 1  Real Madrid", facts, "en"
    )
    assert any("wrong side" in p for p in problems)


def test_an_uncited_player_or_statistic_cannot_enter_a_card(facts):
    for invented in (
        "VERIFIED INFORMATION\nVinicius Junior opened the scoring",
        "VERIFIED INFORMATION\nPossession Real Madrid 64 percent",
    ):
        problems = verified_cards.verify_card_text(invented, facts, "en")
        assert problems, f"invented content accepted: {invented}"
        assert any("unsupported" in p for p in problems)


def test_a_card_may_not_claim_to_be_match_footage(facts):
    for pretending in (
        "VERIFIED INFORMATION\nPhotograph from the match",
        "معلومات موثقة\nلقطة من المباراة",
    ):
        problems = verified_cards.verify_card_text(pretending, facts, "ar")
        assert any("photographic evidence" in p for p in problems)


def test_an_arabic_run_does_not_get_an_english_only_card(facts):
    problems = verified_cards.verify_card_text(
        "VERIFIED INFORMATION\nReal Madrid  2 – 1  Inter Milan", facts, "ar"
    )
    assert any("no Arabic text" in p for p in problems)


def test_an_unreadable_wall_of_text_is_refused(facts):
    body = "معلومات موثقة\n" + ("Real Madrid Inter Milan " * 40)
    assert verified_cards.verify_card_text(body, facts, "ar")


def test_nothing_citable_means_no_card_rather_than_a_filler_claim(tmp_path):
    """A source with a title but no match in it can carry no card."""
    bare = {
        "brief": "Nothing specific was retrieved.",
        "sources": [{"title": "Soccer news", "domain": "example.com"}],
        "verified_facts": [],
        "grounded": True,
    }
    facts = verified_cards.load_grounded_facts(_workspace(tmp_path, bare))
    assert facts is not None
    assert verified_cards.build_card_props(facts, "en") is None


# ── only the failed slots are replaced ───────────────────────────────

def test_only_the_named_beats_become_cards(facts, tmp_path):
    """The 14/9 split: passing beats keep their photographs untouched."""
    script = _script(slot_count=5)
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(1, 6):
        (raw / f"section_001_{i:02d}.jpg").write_bytes(b"x")

    converted = verified_cards.apply_verified_cards(
        script=script,
        config=_config(),
        facts=facts,
        targets={(1, 2), (1, 4)},
        raw_dir=raw,
    )

    assert converted == [(1, 2), (1, 4)]
    visuals = [s.visual for s in script.sections[0].slots]
    assert visuals == [
        "google_photo", "verified_card", "google_photo", "verified_card", "google_photo"
    ]
    # The photographs that passed are still on disk; the refused ones are gone.
    survivors = sorted(p.name for p in raw.glob("*.jpg"))
    assert survivors == [
        "section_001_01.jpg", "section_001_03.jpg", "section_001_05.jpg"
    ]


def test_a_beat_that_becomes_a_card_loses_its_rejected_photo_and_clip(
    facts, tmp_path
):
    """Leaving the refused file behind would ship exactly what the gate barred."""
    script = _script(slot_count=1)
    raw, videos = tmp_path / "raw", tmp_path / "videos"
    raw.mkdir()
    videos.mkdir()
    (raw / "section_001_01.jpg").write_bytes(b"x")
    (videos / "section_001_01.mp4").write_bytes(b"x")

    verified_cards.apply_verified_cards(
        script=script, config=_config(), facts=facts,
        targets={(1, 1)}, raw_dir=raw, videos_dir=videos,
    )

    assert not (raw / "section_001_01.jpg").exists()
    assert not (videos / "section_001_01.mp4").exists()


def test_a_converted_slot_drops_its_sourcing_policy(facts, tmp_path):
    """A stale policy re-failed the whole stage on the real run.

    `photo_backed_info_slide` is only legal on an info_slide, and the sourcer
    raises when it is not -- so a converted beat that kept the policy took
    image_source down with it.
    """
    script = _script(slot_count=2)
    script.sections[0].slots[0].visual_policy = "photo_backed_info_slide"
    script.sections[0].slots[1].visual_policy = "literal_google_photo"

    verified_cards.apply_verified_cards(
        script=script, config=_config(), facts=facts,
        targets={(1, 1)}, raw_dir=tmp_path,
    )

    converted, untouched = script.sections[0].slots
    assert converted.visual_policy == "source_as_written"
    assert converted.visual_policy in VisualSlot.VISUAL_POLICIES
    assert converted.prompt == "" and converted.keywords == ""
    # A beat that was not converted keeps everything it had.
    assert untouched.visual_policy == "literal_google_photo"


def test_a_stale_policy_on_an_existing_card_is_repaired_not_fatal():
    """A resumed run reads a script an earlier pass wrote.

    A card left carrying `photo_backed_info_slide` failed the whole
    image_source stage on resume, twice, before the beat was even looked at.
    """
    from core.image_sourcer import _plan_slot_visual

    class _Section:
        id = 2

    slot = VisualSlot(
        visual="info_card",
        visual_policy="photo_backed_info_slide",
        props={"text": "معلومات موثقة", verified_cards.VERIFIED_CARD_KEY: True},
    )
    policy, rewritten = _plan_slot_visual(section=_Section(), slot=slot)
    assert policy == "source_as_written"
    assert rewritten is True
    assert slot.visual_policy == "source_as_written"


def test_a_real_info_slide_still_enforces_its_policy():
    """The repair is for converted cards only, not a hole in the check."""
    from core.image_sourcer import _plan_slot_visual

    class _Section:
        id = 2

    slot = VisualSlot(
        visual="info_card", visual_policy="photo_backed_info_slide", props={}
    )
    with pytest.raises(ValueError, match="requires info_slide"):
        _plan_slot_visual(section=_Section(), slot=slot)


def test_the_sourcing_log_records_the_card_as_a_card(facts, tmp_path):
    log: list[dict] = []
    verified_cards.apply_verified_cards(
        script=_script(1), config=_config(), facts=facts,
        targets={(1, 1)}, raw_dir=tmp_path, sourcing_log=log,
    )
    assert log[0]["source"] == "verified_info_card (grounded facts)"
    assert log[0]["file"] is None


# ── the stage completes on cards ─────────────────────────────────────

def test_a_card_beat_is_not_expected_to_have_a_sourced_file(facts, tmp_path):
    """Coverage means every beat has a truthful visual, not a photograph."""
    from core.utils import expected_sourced_image_slots

    config = _config()
    assert config.image_sourcing.generate_info_card_illustrations is False
    script = _script(slot_count=3)
    verified_cards.apply_verified_cards(
        script=script, config=config, facts=facts,
        targets={(1, 2), (1, 3)}, raw_dir=tmp_path,
    )
    assert expected_sourced_image_slots(script, config) == [(1, 1)]


def test_the_renderer_draws_a_card_beat_in_its_own_position(facts, tmp_path):
    """Mixed photographs and cards keep their order and their timing."""
    from core import render_sections

    script = _script(slot_count=3)
    verified_cards.apply_verified_cards(
        script=script, config=_config(), facts=facts,
        targets={(1, 2)}, raw_dir=tmp_path,
    )
    slot = script.sections[0].slots[1]
    assert render_sections._VISUAL_TO_COMPONENT[slot.visual] == "VerifiedCard"
    # A component slot: it owns a position and a duration, and needs no file.
    assert slot.visual in VisualSlot.COMPONENT_TYPES
    assert slot.visual not in VisualSlot.SOURCEABLE_TYPES
    # The structured fields the component actually draws from.
    assert slot.props["eyebrow"] == "معلومات موثقة"
    assert slot.props["layout"] in {"scoreline", "fact", "matchup"}
    assert slot.props["source_line"].endswith("espn.com")
    assert slot.props["rtl"] is True


def test_the_football_channel_has_the_floor_switched_on():
    assert _config().image_sourcing.verified_card_fallback is True


def test_other_channels_are_untouched():
    for slug in ("horror_stories", "true_stories", "animated_stories"):
        cfg = ChannelConfig.model_validate(json.loads(
            (__import__("pathlib").Path(f"config/channels/{slug}.json"))
            .read_text(encoding="utf-8")
        ))
        assert cfg.image_sourcing.verified_card_fallback is False, slug


# ── the gate exhausting no longer ends the run ───────────────────────

def _gate_error(rejected: list[tuple[int, int]], *, attempts: int = 2):
    """A ReviewGateError shaped like the one the real gate raises."""
    from core.reviewer import ReviewGateError

    return ReviewGateError("image_review", {
        "approved": False,
        "attempts": attempts,
        "feedback": "several images do not show this match",
        "flagged_for_review": True,
        "review_history": [
            {"attempt": 1, "approved": False, "image_results": [
                {"section_id": s, "sub_image_index": i, "approved": False}
                for s, i in rejected + [(1, 1)]
            ]},
            {"attempt": attempts, "approved": False, "image_results": [
                {"section_id": s, "sub_image_index": i, "approved": False,
                 "suggestion": "try the trophy ceremony"}
                for s, i in rejected
            ] + [{"section_id": 1, "sub_image_index": 1, "approved": True}]},
        ],
    })


@pytest.fixture
def no_generation(monkeypatch):
    """Every generator refuses, so only the card tier can cover a beat."""
    import clients

    async def _refuse(*a, **k):
        return None

    monkeypatch.setattr(clients, "generate_scene_image", _refuse)


@pytest.fixture
def generation_succeeds(monkeypatch):
    """Record what the generators were asked for, and write a usable file."""
    import clients
    from PIL import Image

    calls: list[dict] = []

    async def _draw(prompt, output_path, **kwargs):
        calls.append({"prompt": prompt, "path": Path(output_path), **kwargs})
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1080, 1920), (12, 34, 56)).save(output_path)
        return Path(output_path)

    monkeypatch.setattr(clients, "generate_scene_image", _draw)
    return calls


@pytest.mark.asyncio
async def test_the_exhausted_gate_is_covered_and_the_stage_can_finish(
    facts, tmp_path, no_generation
):
    """The whole point: 9 refused beats stop taking 14 good ones with them."""
    from core import image_sourcer

    script = _script(slot_count=5)
    raw = tmp_path / "raw"
    raw.mkdir()
    for i in range(1, 6):
        (raw / f"section_001_{i:02d}.jpg").write_bytes(b"x")
    log: list[dict] = []

    covered = await image_sourcer._cover_rejected_with_verified_cards(
        gate_error=_gate_error([(1, 3), (1, 5)]),
        script=script,
        config=_config(),
        workspace=_workspace(tmp_path),
        sourcing_log=log,
        raw_dir=raw,
        videos_dir=tmp_path / "videos",
    )

    assert sorted(covered) == [(1, 3), (1, 5)]
    assert [s.visual for s in script.sections[0].slots] == [
        "google_photo", "google_photo", "verified_card", "google_photo", "verified_card"
    ]
    # Beat 1 was rejected on the *first* pass and accepted on the last. Only
    # the final verdict counts, so its photograph survives.
    assert (raw / "section_001_01.jpg").exists()


@pytest.mark.asyncio
async def test_an_identity_free_beat_is_drawn_rather_than_carded(
    facts, tmp_path, generation_succeeds
):
    """A generated stadium beats a text card, and asserts nothing."""
    from core import image_sourcer

    script = _script(slot_count=2)
    script.sections[0].slots[0].keywords = "San Siro stadium floodlights at night"
    script.sections[0].slots[1].keywords = "Rodrygo scores the winning goal"
    raw = tmp_path / "raw"
    raw.mkdir()
    log: list[dict] = []

    covered = await image_sourcer._cover_rejected_with_verified_cards(
        gate_error=_gate_error([(1, 1), (1, 2)]),
        script=script,
        config=_config(max_generated_player_reconstructions=0),
        workspace=_workspace(tmp_path),
        sourcing_log=log,
        raw_dir=raw,
        videos_dir=tmp_path / "videos",
    )

    assert sorted(covered) == [(1, 1), (1, 2)]
    stadium, event = script.sections[0].slots
    assert stadium.visual == "ai_illustration", "the stadium beat was not drawn"
    # A goal being scored is an event claim: never generated, always a card.
    assert event.visual == "verified_card"

    drawn = [c for c in log if str(c["source"]).startswith("generated_safe_visual")]
    assert len(drawn) == 1
    assert drawn[0]["provenance_kind"] == "generated_visual"
    assert "not a record of this match" in drawn[0]["provenance"]


@pytest.mark.asyncio
async def test_the_generation_brief_forbids_recognisable_people(
    facts, tmp_path, generation_succeeds
):
    from core import image_sourcer

    script = _script(slot_count=1)
    script.sections[0].slots[0].keywords = "crowd atmosphere in the stands"
    await image_sourcer._cover_rejected_with_verified_cards(
        gate_error=_gate_error([(1, 1)]),
        script=script, config=_config(max_generated_player_reconstructions=0),
        workspace=_workspace(tmp_path), sourcing_log=[],
        raw_dir=tmp_path / "raw", videos_dir=tmp_path / "videos",
    )
    prompt = generation_succeeds[0]["prompt"].lower()
    for fence in ("no recognisable real people", "no readable text", "not a record"):
        assert fence in prompt


@pytest.mark.asyncio
async def test_a_beat_with_no_description_left_is_not_drawn(
    facts, tmp_path, generation_succeeds
):
    """A real run paid for "Editorial sports illustration: ." — nothing else."""
    from core import image_sourcer

    script = _script(slot_count=1)
    slot = script.sections[0].slots[0]
    slot.prompt, slot.keywords = "", ""

    covered = await image_sourcer._cover_rejected_with_verified_cards(
        gate_error=_gate_error([(1, 1)]),
        script=script, config=_config(max_generated_player_reconstructions=0),
        workspace=_workspace(tmp_path), sourcing_log=[],
        raw_dir=tmp_path / "raw", videos_dir=tmp_path / "videos",
    )

    assert not generation_succeeds, "a generator ran with an empty brief"
    assert covered == [(1, 1)]
    assert slot.visual == "verified_card"


def test_an_empty_brief_yields_no_prompt_at_all():
    from core.image_sourcer import _safe_fallback_prompt

    assert _safe_fallback_prompt("", "") is None
    assert _safe_fallback_prompt("  ", "  ") is None
    assert _safe_fallback_prompt("stadium at night", "") is not None


@pytest.mark.asyncio
async def test_a_named_person_is_never_drawn_without_a_face_reference(
    facts, tmp_path, generation_succeeds, monkeypatch
):
    """Generating "a Real Madrid forward" is how the wrong player appears."""
    from core import image_sourcer

    async def _no_reference(*a, **k):
        return None

    monkeypatch.setattr(image_sourcer, "_face_reference_for", _no_reference)

    script = _script(slot_count=1)
    slot = script.sections[0].slots[0]
    slot.keywords = "Alessandro Bastoni tackle"
    slot.props = {"football_subject": "player", "football_player_name": "Alessandro Bastoni"}

    covered = await image_sourcer._cover_rejected_with_verified_cards(
        gate_error=_gate_error([(1, 1)]),
        script=script, config=_config(), workspace=_workspace(tmp_path),
        sourcing_log=[], raw_dir=tmp_path / "raw", videos_dir=tmp_path / "videos",
    )

    assert covered == [(1, 1)]
    assert slot.visual == "verified_card", "a person was drawn with no identity reference"
    assert not generation_succeeds, "a generator ran for an unreferenced person"


@pytest.mark.asyncio
async def test_a_referenced_person_is_reconstructed_and_marked_as_such(
    facts, tmp_path, generation_succeeds, monkeypatch
):
    from core import image_sourcer
    from PIL import Image

    face = tmp_path / "face.jpg"
    Image.new("RGB", (512, 512), (200, 200, 200)).save(face)

    async def _reference(name, **k):
        assert name == "Alessandro Bastoni"
        return face

    monkeypatch.setattr(image_sourcer, "_face_reference_for", _reference)

    script = _script(slot_count=1)
    slot = script.sections[0].slots[0]
    slot.keywords = "Alessandro Bastoni tackle"
    slot.props = {
        "football_subject": "player",
        "football_player_name": "Alessandro Bastoni",
        "football_current_club": "Inter Milan",
    }
    log: list[dict] = []

    covered = await image_sourcer._cover_rejected_with_verified_cards(
        gate_error=_gate_error([(1, 1)]),
        script=script, config=_config(), workspace=_workspace(tmp_path),
        sourcing_log=log, raw_dir=tmp_path / "raw", videos_dir=tmp_path / "videos",
    )

    assert covered == [(1, 1)]
    assert slot.visual == "ai_illustration"
    record = [c for c in log if c["source"] == "generated_person_reconstruction"][0]
    assert record["person"] == "Alessandro Bastoni"
    assert record["provenance_kind"] == "generated_reconstruction"
    assert "not a photograph" in record["provenance"]

    call = generation_succeeds[-1]
    assert call["reference_image"] == face, "the face was not handed to the model"
    assert "Inter Milan colours" in call["prompt"]
    assert "not documentary photography" in call["prompt"]


@pytest.mark.asyncio
async def test_a_channel_without_the_floor_still_fails(facts, tmp_path):
    from core import image_sourcer

    covered = await image_sourcer._cover_rejected_with_verified_cards(
        gate_error=_gate_error([(1, 2)]),
        script=_script(3),
        config=_config(verified_card_fallback=False),
        workspace=_workspace(tmp_path),
        sourcing_log=[],
        raw_dir=tmp_path,
        videos_dir=tmp_path,
    )
    assert covered == []


@pytest.mark.asyncio
async def test_an_ungrounded_run_fails_terminally_rather_than_inventing_a_card(
    tmp_path,
):
    from core import image_sourcer

    error = _gate_error([(1, 2)])
    covered = await image_sourcer._cover_rejected_with_verified_cards(
        gate_error=error,
        script=_script(3),
        config=_config(),
        workspace=_workspace(tmp_path, dict(RESEARCH, grounded=False)),
        sourcing_log=[],
        raw_dir=tmp_path,
        videos_dir=tmp_path,
    )
    assert covered == []
    assert error.factual_limit is True


@pytest.mark.asyncio
async def test_an_unattributable_rejection_is_not_guessed_at(facts, tmp_path):
    """Without per-beat detail there is no way to tell good from bad."""
    from core import image_sourcer
    from core.reviewer import ReviewGateError

    blind = ReviewGateError("image_review", {
        "approved": False, "attempts": 2, "feedback": "not good enough",
        "review_history": [{"attempt": 2, "approved": False}],
    })
    script = _script(3)
    covered = await image_sourcer._cover_rejected_with_verified_cards(
        gate_error=blind, script=script, config=_config(),
        workspace=_workspace(tmp_path), sourcing_log=[],
        raw_dir=tmp_path, videos_dir=tmp_path,
    )
    assert covered == []
    assert all(s.visual == "google_photo" for s in script.sections[0].slots)


# ── several cards in one section ─────────────────────────────────────

def test_consecutive_cards_in_a_section_are_not_the_same_panel(facts, tmp_path):
    """Section 2 of the real run showed four identical cards in a row."""
    script = _script(slot_count=4)
    verified_cards.apply_verified_cards(
        script=script, config=_config(), facts=facts,
        targets={(1, 1), (1, 2), (1, 3), (1, 4)}, raw_dir=tmp_path,
    )
    texts = [s.props["text"] for s in script.sections[0].slots]
    assert len(set(texts)) == 4, "the same card was repeated"


def test_every_variant_stays_inside_the_citations(facts):
    for variant in range(6):
        props = verified_cards.build_card_props(facts, "ar", variant=variant)
        assert props is not None
        assert verified_cards.verify_card_text(props["text"], facts, "ar") == []


def test_a_variant_cannot_introduce_a_field_the_run_lacks(tmp_path):
    """With no date cited, no variant may print one."""
    no_date = dict(
        RESEARCH,
        brief="- [espn.com] Real Madrid 2-1 Inter Milan Uefa Champions League",
        verified_facts=[{"claim": "Real Madrid 2-1 Inter Milan"}],
        sources=[{"title": "Real Madrid 2-1 Inter Milan", "domain": "espn.com"}],
    )
    facts = verified_cards.load_grounded_facts(_workspace(tmp_path, no_date))
    assert facts.date_text == ""
    for variant in range(6):
        props = verified_cards.build_card_props(facts, "en", variant=variant)
        assert props is not None
        assert verified_cards.verify_card_text(props["text"], facts, "en") == []


def test_a_verified_card_does_not_starve_the_photographs_beside_it(facts, tmp_path):
    """The prose-card minimum squeezed a real photo down to a single frame."""
    from core import render_sections

    script = _script(slot_count=8)
    verified_cards.apply_verified_cards(
        script=script, config=_config(), facts=facts,
        targets={(1, 1), (1, 4), (1, 7)}, raw_dir=tmp_path,
    )
    section = script.sections[0]
    section.actual_duration_seconds = 18.9
    even = [18.9 / 8] * 8

    adjusted = render_sections._enforce_component_minimums(
        section, even, min_seconds=5.0, donor_min=2.5
    )

    assert sum(adjusted) <= 18.9 + 0.01, "the section grew past its narration"
    assert min(adjusted) >= 2.0, f"a beat was starved: {adjusted}"


def test_a_prose_info_card_keeps_the_longer_minimum():
    """The shorter minimum is for verified cards only."""
    from core import render_sections

    prose = VisualSlot(visual="info_card", props={"text": "a paragraph of prose"})
    card = VisualSlot(visual="info_card", props={"text": "x", "verified_card": True})
    assert render_sections._component_minimum(prose, 5.0, 2.5) == 5.0
    assert render_sections._component_minimum(card, 5.0, 2.5) == 3.0
    assert render_sections._component_minimum(None, 5.0, 2.5) == 5.0


# ── the card is a designed graphic, not a placeholder ────────────────

def test_the_card_ships_structured_fields_not_a_text_blob(facts):
    """The renderer draws a designed panel, so it needs the parts, not prose."""
    props = verified_cards.build_card_props(facts, "ar")
    for field in ("layout", "eyebrow", "home", "away", "home_score",
                  "away_score", "competition", "date_text", "source_line", "rtl"):
        assert field in props, field
    assert props["home_score"] == "2" and props["away_score"] == "1"


def test_an_arabic_run_gets_a_right_to_left_card(facts):
    assert verified_cards.build_card_props(facts, "ar")["rtl"] is True
    assert verified_cards.build_card_props(facts, "en")["rtl"] is False


def test_every_card_names_its_source(facts):
    """A card with no visible source is not a verified card."""
    for variant in range(6):
        props = verified_cards.build_card_props(facts, "ar", variant=variant)
        assert props["source_line"].endswith("espn.com")


def test_the_card_carries_no_logo_or_player_imagery(facts):
    """No crest, no likeness, nothing generated — it is type on colour."""
    props = verified_cards.build_card_props(facts, "ar")
    for forbidden in ("illustration_url", "logo", "crest", "badge", "image_url"):
        assert forbidden not in props


def test_the_component_is_registered_on_both_sides():
    from core import render_sections
    from pathlib import Path as P

    assert render_sections._VISUAL_TO_COMPONENT["verified_card"] == "VerifiedCard"
    registry = (
        P("rendering/remotion/src/components/SectionComposition.tsx")
        .read_text(encoding="utf-8")
    )
    assert "import { VerifiedCard }" in registry
    assert "\n  VerifiedCard,\n" in registry


def test_a_card_from_an_older_version_is_redrawn_in_the_current_design(
    facts, tmp_path
):
    """A resumed workspace holds cards written before the design existed.

    Nothing else would ever redraw them: the refresh path only runs when the
    review gate fails, and a run whose photographs all pass never gets there.
    """
    script = _script(slot_count=2)
    legacy, photo = script.sections[0].slots
    legacy.visual = "info_card"
    legacy.visual_policy = "photo_backed_info_slide"
    legacy.prompt, legacy.keywords = "", ""
    legacy.props = {
        "text": "معلومات موثقة\nReal Madrid  2 – 1  Inter Milan",
        verified_cards.VERIFIED_CARD_KEY: True,
        "original_keywords": "scoreboard",
    }

    migrated = verified_cards.migrate_legacy_cards(script, _config(), facts)

    assert migrated == 1
    assert legacy.visual == "verified_card"
    assert legacy.visual_policy == "source_as_written"
    assert legacy.props["layout"] in {"scoreline", "fact", "matchup"}
    assert legacy.props["eyebrow"] == "معلومات موثقة"
    # What the beat was about survives the redraw.
    assert legacy.props["original_keywords"] == "scoreboard"
    # A normal photo beat is untouched.
    assert photo.visual == "google_photo"


def test_migration_is_idempotent(facts, tmp_path):
    script = _script(slot_count=1)
    verified_cards.apply_verified_cards(
        script=script, config=_config(), facts=facts,
        targets={(1, 1)}, raw_dir=tmp_path,
    )
    assert verified_cards.migrate_legacy_cards(script, _config(), facts) == 0


def test_migration_does_nothing_on_an_ungrounded_run():
    script = _script(slot_count=1)
    assert verified_cards.migrate_legacy_cards(script, _config(), None) == 0


# ── no card walls ────────────────────────────────────────────────────

def test_neighbouring_cards_do_not_repeat_the_same_layout(facts, tmp_path):
    """Section 2 of the real run showed four identical panels in a row."""
    script = _script(slot_count=6)
    verified_cards.apply_verified_cards(
        script=script, config=_config(), facts=facts,
        targets={(1, i) for i in range(1, 7)}, raw_dir=tmp_path,
    )
    layouts = [s.props["layout"] for s in script.sections[0].slots]
    for a, b in zip(layouts, layouts[1:]):
        assert a != b, f"two adjacent cards share a layout: {layouts}"


def test_layout_variation_continues_across_a_section_boundary(facts, tmp_path):
    """Per-section numbering restarted, so boundary cards matched."""
    script = Script(
        title="t", video_type="news",
        sections=[
            ScriptSection(id=1, narration="n", slots=[
                VisualSlot(visual="google_photo", keywords="k")]),
            ScriptSection(id=2, narration="n", slots=[
                VisualSlot(visual="google_photo", keywords="k")]),
        ],
    )
    verified_cards.apply_verified_cards(
        script=script, config=_config(), facts=facts,
        targets={(1, 1), (2, 1)}, raw_dir=tmp_path,
    )
    last_of_first = script.sections[0].slots[0].props["layout"]
    first_of_next = script.sections[1].slots[0].props["layout"]
    assert last_of_first != first_of_next


def test_a_layout_is_never_used_without_the_facts_it_needs(tmp_path):
    """A scoreline layout with no cited score would render an empty panel."""
    no_score = {
        "brief": "- [espn.com] Real Madrid meet Inter Milan in the Champions League",
        "sources": [{"title": "Real Madrid v Inter Milan", "domain": "espn.com"}],
        "verified_facts": [{"claim": "Real Madrid meet Inter Milan"}],
        "grounded": True,
    }
    facts = verified_cards.load_grounded_facts(_workspace(tmp_path, no_score))
    assert facts.has_scoreline is False
    for variant in range(6):
        props = verified_cards.build_card_props(facts, "en", variant=variant)
        assert props is not None
        assert props["layout"] != "scoreline"


# ── conversion is not one-way ────────────────────────────────────────

def test_a_card_remembers_what_the_beat_was_about(facts, tmp_path):
    script = _script(slot_count=1)
    slot = script.sections[0].slots[0]
    slot.prompt = "San Siro under floodlights"
    slot.keywords = "San Siro stadium floodlights"

    verified_cards.apply_verified_cards(
        script=script, config=_config(), facts=facts,
        targets={(1, 1)}, raw_dir=tmp_path,
    )

    assert slot.props["original_keywords"] == "San Siro stadium floodlights"
    assert slot.props["original_prompt"] == "San Siro under floodlights"

    # And a second pass over the same beat -- what a resume does -- must not
    # lose them. It did: the refresh built a fresh props dict and read the
    # originals out of that instead of out of the slot.
    verified_cards.apply_verified_cards(
        script=script, config=_config(), facts=facts, targets=set(), raw_dir=tmp_path
    )
    assert slot.props["original_keywords"] == "San Siro stadium floodlights"
    assert slot.props["original_prompt"] == "San Siro under floodlights"


@pytest.mark.asyncio
async def test_an_existing_card_is_reclaimed_by_the_drawing_tier(
    facts, tmp_path, generation_succeeds
):
    """A beat carded before this tier existed is still a stadium beat.

    This is the state a resumed run is actually in, and without it every card
    an earlier pass wrote stays a card forever -- which is what the final gate
    complained about ("multiple frames use generic solid-colored backgrounds").
    """
    from core import image_sourcer

    script = _script(slot_count=2)
    stadium, event = script.sections[0].slots
    stadium.prompt, stadium.keywords = "", ""
    stadium.props = {
        "text": "معلومات موثقة", verified_cards.VERIFIED_CARD_KEY: True,
        "original_keywords": "San Siro stadium floodlights", "original_prompt": "",
    }
    stadium.visual = "verified_card"
    # A real carded slot has no prompt/keywords left on it; the beat's own
    # words live in props, which is exactly what the reclaim path reads.
    event.prompt, event.keywords = "", ""
    event.props = {
        "text": "معلومات موثقة", verified_cards.VERIFIED_CARD_KEY: True,
        "original_keywords": "Rodrygo scores the winning goal", "original_prompt": "",
    }
    event.visual = "verified_card"

    await image_sourcer._cover_rejected_with_verified_cards(
        gate_error=_gate_error([(1, 1)]),
        script=script, config=_config(max_generated_player_reconstructions=0),
        workspace=_workspace(tmp_path), sourcing_log=[],
        raw_dir=tmp_path / "raw", videos_dir=tmp_path / "videos",
    )

    assert stadium.visual == "ai_illustration", "the stadium card was not reclaimed"
    assert stadium.keywords == "San Siro stadium floodlights"
    assert stadium.props == {}
    # The event card is not reclaimable: a generated goal is a fabricated
    # record whatever tier asks for it.
    assert event.visual == "verified_card"


# ── recoverable vs terminal ──────────────────────────────────────────

def test_a_provider_failure_is_recoverable_and_keeps_its_checkpoint():
    attempt = RunAttempt()
    attempt.finish(recoverable=True, reason="provider refused")
    assert attempt.failure_kind is FailureKind.RECOVERABLE
    assert attempt.result_status == reliability.RESULT_RECOVERABLE_FAILED


def test_a_factual_limit_is_terminal():
    attempt = RunAttempt()
    attempt.finish(recoverable=False, reason="no cited fact", kind=FailureKind.FACTUAL)
    assert attempt.result_status == reliability.RESULT_TERMINAL_FAILED


def test_a_completed_run_is_neither():
    attempt = RunAttempt()
    attempt.complete()
    assert attempt.result_status == reliability.RESULT_COMPLETED
    assert attempt.failure_kind is None


def test_resuming_clears_the_previous_verdict():
    attempt = RunAttempt()
    attempt.finish(recoverable=True, reason="timeout")
    attempt.begin_resume()
    assert attempt.failure_kind is None
    assert attempt.retry_count == 1


def test_backoff_is_bounded_and_never_zero():
    delays = [reliability.retry_delay_seconds(n) for n in range(8)]
    assert all(60.0 <= d <= 300.0 for d in delays)
    assert delays[0] < delays[1], "backoff does not grow"
    assert delays[-1] == delays[3], "backoff is not capped"
    # The first wait must outlast the model quota cooldown, or the resume
    # walks straight back into the refusal that stopped it.
    import clients
    assert delays[0] >= clients._QUOTA_COOLDOWN_SECONDS


def test_the_retry_ceiling_is_finite():
    import worker as worker_mod
    assert 1 <= worker_mod.MAX_RECOVERY_ATTEMPTS <= 10


# ── what the customer is told ────────────────────────────────────────

def test_a_factual_failure_asks_the_customer_for_what_is_missing():
    message = reliability.customer_failure_message(FailureKind.FACTUAL, will_retry=False)
    assert "couldn't verify this match" in message
    assert "date" in message and "competition" in message


def test_a_provider_failure_does_not_blame_the_topic():
    message = reliability.customer_failure_message(
        FailureKind.RECOVERABLE, will_retry=True
    )
    assert "progress is saved" in message.lower()
    assert "retry automatically" in message.lower()
    assert message != reliability.FACTUAL_MESSAGE
    assert "verify" not in message.lower()


def test_an_exhausted_run_stops_promising_a_retry_that_is_not_coming():
    message = reliability.customer_failure_message(
        FailureKind.RECOVERABLE, will_retry=False
    )
    assert "retry automatically" not in message.lower()
    # ...and still does not blame the topic.
    assert "topic is fine" in message.lower()


def test_the_two_failure_messages_differ():
    factual = reliability.customer_failure_message(FailureKind.FACTUAL, will_retry=False)
    provider = reliability.customer_failure_message(
        FailureKind.RECOVERABLE, will_retry=True
    )
    assert factual != provider


def test_no_customer_message_leaks_anything_internal():
    leaks = (
        "gemini", "serper", "pexels", "openai", "429", "quota", "traceback",
        "image_source", "image_review", "script_review", "exception", "http",
        "api", "token", "workspace",
    )
    messages = [
        *reliability.PROGRESS_MESSAGES.values(),
        reliability.DEGRADED_MESSAGE,
        reliability.RECOVERABLE_MESSAGE,
        reliability.FACTUAL_MESSAGE,
        reliability.EXHAUSTED_MESSAGE,
    ]
    for message in messages:
        low = message.lower()
        for leak in leaks:
            assert leak not in low, f"{leak!r} leaked in {message!r}"


def test_the_progress_states_cover_every_stage_the_worker_reports():
    import worker as worker_mod

    for stage in worker_mod.STAGE_PERCENT:
        assert stage in reliability.PROGRESS_MESSAGES, stage
    assert worker_mod.STAGE_PROGRESS["image_source"][1] == "Finding match visuals…"
    assert worker_mod.STAGE_PROGRESS["planning"][1] == "Researching verified sources…"


def test_a_degraded_provider_is_announced_without_naming_it():
    import worker as worker_mod

    line = "2026-09-11 21:53:19 | WARNING | [recovery] degraded provider; continuing on another source"
    assert worker_mod._DEGRADED_RE.search(line)
    assert "another source" in reliability.DEGRADED_MESSAGE
    # The line the pipeline actually prints must not carry the provider name.
    assert "gemini" not in line.lower()


def test_the_fallback_visual_state_is_recognised():
    import worker as worker_mod

    assert worker_mod._FALLBACK_VISUALS_RE.search(
        "INFO | [recovery] building-fallback-visuals"
    )
    assert reliability.PROGRESS_MESSAGES["process"] == "Building remaining visuals…"


# ── the final gate must not overrule the citations either ────────────

def test_the_final_gate_is_given_the_run_citations(tmp_path):
    import prompts

    prompt = prompts.package_review_prompt(
        title="t", description="d", tags=[], video_type="news",
        narration_summary="n",
        grounded_brief=verified_cards.grounded_brief(_workspace(tmp_path)),
    )
    assert "VERIFIED FACTS FOR THIS RUN" in prompt
    assert "Real Madrid 2-1 Inter Milan" in prompt


def test_the_final_gate_is_told_not_to_call_a_cited_date_impossible():
    """It rejected the finished video for showing the cited 8 Sep 2026."""
    import prompts

    prompt = prompts.package_review_prompt(
        title="t", description="d", tags=[], video_type="news",
        narration_summary="n", grounded_brief=BRIEF_LINE,
    ).lower()
    assert "in the future" in prompt
    assert "outrank your own" in prompt


def test_the_final_gate_must_still_reject_what_is_not_cited():
    import prompts

    prompt = prompts.package_review_prompt(
        title="t", description="d", tags=[], video_type="news",
        narration_summary="n", grounded_brief=BRIEF_LINE,
    )
    assert "must still reject" in prompt
    for unsupported in ("invented statistic", "wrong person", "different scoreline"):
        assert unsupported in prompt


def test_an_ungrounded_run_gets_no_verified_facts_block(tmp_path):
    import prompts

    assert verified_cards.grounded_brief(tmp_path) == ""
    prompt = prompts.package_review_prompt(
        title="t", description="d", tags=[], video_type="news",
        narration_summary="n", grounded_brief="",
    )
    assert "VERIFIED FACTS FOR THIS RUN" not in prompt


def test_the_final_gate_brief_comes_from_research_not_the_script():
    import inspect
    import factory

    source = inspect.getsource(factory._run_final_review)
    assert "grounded_brief(workspace)" in source
    assert "grounded_brief=research_brief" in source


# ── nothing was weakened ─────────────────────────────────────────────

def test_the_image_review_gate_is_still_unwaivable_and_still_fails_closed():
    import factory
    from core import reviewer
    import inspect

    assert "image_review" in factory._UNWAIVABLE_REVIEW_GATES
    # The gate still raises when it runs out of attempts; the floor is built
    # around that exception, not by removing it.
    source = inspect.getsource(reviewer.review_gate)
    assert "raise ReviewGateError(gate_name, result)" in source


def test_the_floor_never_re_admits_a_photograph_the_gate_refused():
    """Cards replace rejected pictures; they never approve them."""
    import inspect
    from core import image_sourcer

    source = inspect.getsource(image_sourcer._cover_rejected_with_verified_cards)
    # It reads the rejections and converts them...
    assert "_rejected_slot_keys(_last_review(gate_error))" in source
    # ...and refuses to act at all when it cannot tell which beat failed.
    assert "leaving the gate failure in place rather than guessing" in source


def test_the_floor_is_opt_in_per_channel():
    import inspect
    from core import image_sourcer

    source = inspect.getsource(image_sourcer._cover_rejected_with_verified_cards)
    assert 'getattr(config.image_sourcing, "verified_card_fallback", False)' in source


def test_an_ungrounded_review_failure_stays_terminal():
    import inspect
    from core import image_sourcer

    source = inspect.getsource(image_sourcer._cover_rejected_with_verified_cards)
    assert "gate_error.factual_limit = True" in source
    factory_source = inspect.getsource(__import__("factory"))
    assert 'getattr(result, "factual_limit", False)' in factory_source
