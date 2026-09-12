"""AI Video Maker: the lifecycle, and the ways it must refuse to die.

The product promise is narrow and testable: a topic goes in, a valid MP4 comes
out, and the things that routinely go wrong in the middle -- a provider being
down, one clip 404ing, a search returning nothing, a worker restarting -- cost
that one thing rather than the whole video.

Everything here runs on fixtures. No provider is called, no credit is spent,
and the suite finishes in about a second.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from aivideo import footage, pipeline, rank as ranking, script as script_stage, subtitles, voice
from aivideo.spec import (
    CAPTION_PRESETS,
    CaptionStyle,
    VideoSpec,
    normalise_spec,
    voices_for,
)


# ── the spec the whole product agrees on ─────────────────────────────

def test_a_spec_survives_junk_from_the_client():
    """Clamped, never rejected: a stored preference must not fail a job."""
    spec = normalise_spec({
        "topic": "  5 crazy facts about Dubai  ",
        "language": "klingon",
        "duration_seconds": 35,
        "aspect_ratio": "4:3",
        "voice": "not-a-voice",
        "voice_rate": 9.0,
        "clip_seconds": 0.1,
        "captions": {"preset": "nope", "size": 999, "position": "sideways"},
    })
    assert spec.topic == "5 crazy facts about Dubai"
    assert spec.language == "en"
    assert spec.duration_seconds == 30          # snapped to the nearest offered
    assert spec.aspect_ratio == "9:16"
    assert spec.voice in {v["id"] for v in voices_for("en")}
    assert spec.voice_rate == 2.0
    assert spec.clip_seconds == 1.5
    assert spec.captions.size == 140
    assert spec.captions.position == "bottom"


def test_the_offered_durations_and_ratios_are_what_the_ui_shows():
    from aivideo.spec import ASPECT_SIZES, DURATIONS

    assert DURATIONS == (30, 45, 60, 90)
    assert set(ASPECT_SIZES) == {"9:16", "16:9", "1:1"}
    assert ASPECT_SIZES["9:16"] == (1080, 1920)


def test_every_caption_preset_is_complete_and_renderable():
    for name in CAPTION_PRESETS:
        style = CaptionStyle.from_preset(name)
        assert style.size > 0
        assert style.text_color.startswith("#")
        assert style.position in {"top", "center", "bottom"}
        assert style.words_per_line >= 1


def test_advanced_overrides_beat_the_preset():
    spec = normalise_spec({
        "topic": "t",
        "captions": {"preset": "viral", "text_color": "#FF0000", "size": 50},
    })
    assert spec.captions.preset == "viral"
    assert spec.captions.text_color == "#FF0000"
    assert spec.captions.size == 50


# ── Arabic ───────────────────────────────────────────────────────────

def test_arabic_is_forced_onto_a_font_that_can_shape_it():
    """A Latin-only font renders Arabic as disconnected boxes, silently."""
    from aivideo.spec import ARABIC_CAPABLE_FONTS

    spec = normalise_spec({
        "topic": "حقائق عن دبي",
        "language": "ar",
        "captions": {"preset": "cinematic"},     # playfair: Latin only
    })
    assert spec.captions.font in ARABIC_CAPABLE_FONTS
    assert spec.captions.uppercase is False      # Arabic has no letter case
    assert spec.voice in {v["id"] for v in voices_for("ar")}


def test_every_arabic_font_can_draw_every_letter():
    """A font missing a presentation form loses that letter, silently.

    Tajawal was the default here and carried only 116 of the Arabic
    presentation forms -- every *isolated* form was absent, so a word starting
    with alef, beh, noon or jeem rendered its first letter as a tofu box in
    the finished MP4. Nothing raised, nothing logged; it was only visible by
    looking at a frame.

    This is that check, done from the font's own character map, for every
    family a customer can pick for Arabic.
    """
    pytest.importorskip("fontTools")
    import arabic_reshaper
    from fontTools.ttLib import TTFont

    from medialab import arabic as ar

    reshaper = arabic_reshaper.ArabicReshaper(
        configuration={"delete_harakat": False, "support_ligatures": False}
    )
    # Every Arabic letter, in each joining context, plus the lam-alef pairs.
    letters = "".join(chr(cp) for cp in range(0x0621, 0x064B))
    probe = " ".join(f"{a}{b}{a}" for a in letters for b in "لام")
    needed = {ord(ch) for ch in reshaper.reshape(probe) if 0xFB50 <= ord(ch) <= 0xFEFF}
    assert len(needed) > 80, "the probe did not exercise the presentation forms"

    for family in ar.ARABIC_FONTS:
        path = ar.font_file(family)
        if path is None:
            continue          # a system fallback this machine does not have
        covered: set[int] = set()
        for table in TTFont(str(path))["cmap"].tables:
            covered |= set(table.cmap)
        missing = needed - covered
        assert not missing, (
            f"{family} ({path.name}) cannot draw {len(missing)} Arabic forms, "
            f"starting with {''.join(chr(cp) for cp in sorted(missing)[:8])!r}"
        )


def test_arabic_text_is_joined_but_left_in_logical_order():
    """Joining is ours; the right-to-left reordering is libass's.

    Doing both is what produced the first round of broken captions: libass
    (built here with fribidi) reverses a second time, so every letter ends up
    drawn in the form it had before the reversal and the words render visibly
    disconnected.
    """
    plain = "ريال مدريد"
    shaped = subtitles.shape_arabic(plain)
    assert shaped != plain, "Arabic was not shaped; letters will not join"
    assert any("ﹰ" <= ch <= "﻿" for ch in shaped), "no joined presentation forms"
    # Logical order intact. Reshaping is character-for-character, so the two
    # words keep their lengths; a bidi pass would have swapped them and the
    # 4-letter word would now be second.
    assert [len(word) for word in shaped.split()] == [4, 5], (
        "the words were reordered; libass would then reorder them again"
    )


def test_latin_text_is_left_alone_by_the_shaper():
    assert subtitles.shape_arabic("Dubai skyline") == "Dubai skyline"


def test_an_arabic_caption_file_is_written_with_a_real_font(tmp_path):
    words = [voice.Word("مرحبا", 0.0, 0.5), voice.Word("بالعالم", 0.5, 1.2)]
    style = CaptionStyle.from_preset("bold", "ar")
    out = subtitles.build_ass(words, style, (1080, 1920), tmp_path / "c.ass",
                              language="ar")
    assert out is not None
    body = out.read_text(encoding="utf-8")
    assert "PlayResX: 1080" in body
    assert "Dialogue:" in body
    name, path = subtitles.resolve_font(style.font)
    assert path is not None, "no Arabic-capable font file found on this machine"


# ── captions ─────────────────────────────────────────────────────────

def test_lines_break_on_sentence_ends_as_well_as_word_count():
    words = [
        voice.Word("One", 0, 1), voice.Word("two.", 1, 2),
        voice.Word("Three", 2, 3), voice.Word("four", 3, 4),
        voice.Word("five", 4, 5), voice.Word("six", 5, 6),
    ]
    lines = subtitles.group_lines(words, per_line=4)
    assert lines[0].text == "One two."           # broke early on the full stop
    assert lines[0].start == 0 and lines[0].end == 2


def test_colours_convert_to_ass_bgr_with_inverted_alpha():
    assert subtitles._ass_colour("#FFFFFF") == "&H00FFFFFF"
    assert subtitles._ass_colour("#FF0000") == "&H000000FF"   # BGR, not RGB
    assert subtitles._ass_colour("#00000080").startswith("&H7F")


def test_captions_can_be_turned_off(tmp_path):
    style = CaptionStyle(enabled=False)
    assert subtitles.build_ass(
        [voice.Word("hi", 0, 1)], style, (1080, 1920), tmp_path / "c.ass"
    ) is None


# ── footage: one failure is one clip ─────────────────────────────────

def test_a_search_that_finds_nothing_is_broadened_not_abandoned():
    assert footage.simplify("abandoned desert highway") == "desert highway"
    assert footage.simplify("desert highway") == "highway"
    assert footage.simplify("highway") == ""


@pytest.mark.asyncio
async def test_ranking_a_beat_is_charged_to_the_run(tmp_path, monkeypatch):
    """Ranking is the biggest line in a generation and was going unbilled.

    The script call alone was reported as the cost of a video, which
    understated it by roughly an order of magnitude once there is a vision
    call per beat.
    """
    from aivideo import rank as ranking
    from aivideo.cost import CostLedger

    frame = tmp_path / "f.jpg"
    frame.write_bytes(b"x" * 100)

    async def review(prompt, images, *, operation_label="", on_usage=None, **kwargs):
        on_usage("gemini-3-flash-preview", 6950, 300)
        return {"scores": [{"n": 1, "score": 9, "why": "on subject"}]}

    monkeypatch.setattr(ranking.clients, "review_with_vision", review)

    ledger = CostLedger()
    scored = await ranking.rank_candidates([frame], intent="a reef", ledger=ledger)

    assert scored[0].score == 9
    assert ledger.total_usd > 0
    item = ledger.summary()["items"][0]
    assert item["label"] == "footage relevance"
    assert item["input_tokens"] == 6950


def _beat(shows: str, *terms: str) -> script_stage.Beat:
    return script_stage.Beat(says=shows, shows=shows, terms=list(terms) or [shows])


@pytest.fixture
def stub_candidates(monkeypatch):
    """Stand in for the provider search, the ranker and the frame sampler.

    Candidate collection, ranking and fingerprinting all reach outside the
    process. Patching them here is what keeps this suite offline and fast --
    the previous version patched `fetch_one`, which `gather` no longer calls,
    so the tests were silently making real provider requests.
    """
    from medialab import fingerprint, shots

    plan: dict = {"pools": {}, "scores": {}, "hashes": {}}

    async def collect(term_list, scratch, *, client, portrait, wanted, providers=None):
        scratch.mkdir(parents=True, exist_ok=True)
        plan.setdefault("queries", []).append(list(term_list))
        key = term_list[0] if term_list else ""
        clips = []
        for i, spec in enumerate(plan["pools"].get(key, [])):
            path = scratch / f"cand_{i:02d}.mp4"
            path.write_bytes(b"x" * 60_000)
            clips.append(footage.Clip(
                path, spec.get("provider", "pexels"), key, 1080, 1920, 6.0,
                source_url=spec.get("url", f"{key}-{i}"),
            ))
        return clips

    async def rank_candidates(
        frames, *, intent, says="", operation_label="", ledger=None
    ):
        from aivideo.rank import Scored

        scores = plan["scores"].get(intent)
        if scores is None:
            return [Scored(i, 9) for i in range(len(frames))]
        # A list of lists means "these scores, then those": the second entry
        # is what the broadened second attempt at the same beat gets back.
        if scores and isinstance(scores[0], list):
            scores = scores.pop(0) if len(scores) > 1 else scores[0]
        return [Scored(i, s) for i, s in enumerate(scores[: len(frames)])]

    def sample_frames(video, count=3):
        from PIL import Image

        return [Image.new("RGB", (64, 64), (7, 7, 7)) for _ in range(count)]

    def signature_for(video, count=3):
        # Cryptographically spread by default, so two clips are only "similar"
        # when the test says so. A plain encoding of the filename put
        # "beat_00cand_00" and "beat_02cand_00" one bit apart, which the
        # 10-bit near-duplicate threshold correctly -- and unhelpfully --
        # treated as the same shot.
        import hashlib

        seed = f"{video.parent.name}/{video.stem}".encode()
        default = int.from_bytes(hashlib.sha256(seed).digest()[:8], "big")
        digest = plan["hashes"].get(video.parent.name, default)
        return fingerprint.ClipSignature(path=video, hashes=[digest], width=1080, height=1920)

    # The fallback ladder reaches the model and the photo APIs. Off by
    # default so the suite stays offline; a test that wants a rung sets
    # plan["alternatives"] / plan["stills"] and gets it.
    from aivideo import fallback

    async def propose_alternatives(intents, *, ledger=None):
        plan.setdefault("concept_calls", []).append(list(intents))
        return dict(plan.get("alternatives") or {})

    async def still_candidates(client, terms, scratch, *, portrait, wanted=4, says=""):
        scratch.mkdir(parents=True, exist_ok=True)
        out = []
        for i, spec in enumerate(plan.get("stills", {}).get(terms[0] if terms else "", [])):
            path = scratch / f"still_{i:02d}.jpg"
            path.write_bytes(b"x" * 30_000)
            out.append({**spec, "path": path, "term": terms[0]})
        return out

    def motion_clip(image, target, *, size, seconds=4.0, direction=0):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 60_000)
        return target

    async def generated_still(concept, target, *, portrait, ledger=None):
        if not plan.get("allow_generated"):
            return None
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 40_000)
        plan.setdefault("generated", []).append(concept)
        return target

    monkeypatch.setattr(fallback, "propose_alternatives", propose_alternatives)
    monkeypatch.setattr(fallback, "still_candidates", still_candidates)
    monkeypatch.setattr(fallback, "motion_clip", motion_clip)
    monkeypatch.setattr(fallback, "generated_still", generated_still)
    monkeypatch.setattr(footage, "_collect_candidates", collect)
    monkeypatch.setattr("aivideo.rank.rank_candidates", rank_candidates)
    monkeypatch.setattr(fingerprint, "sample_frames", sample_frames)
    monkeypatch.setattr(fingerprint, "signature_for", signature_for)
    monkeypatch.setattr(shots, "longest_clean_window", lambda v, *, wanted, shots=None: (0.0, wanted))
    return plan


@pytest.mark.asyncio
async def test_one_dead_beat_does_not_lose_the_others(tmp_path, stub_candidates):
    stub_candidates["pools"] = {
        "dubai skyline": [{"url": "a"}],
        "broken thing": [],
        "desert road": [{"url": "b"}],
    }
    clips, missing = await footage.gather(
        [_beat("dubai skyline"), _beat("broken thing"), _beat("desert road")],
        tmp_path,
    )
    assert len(clips) == 2
    assert missing == ["broken thing"]


@pytest.mark.asyncio
async def test_a_beat_that_raises_is_contained(tmp_path, stub_candidates, monkeypatch):
    async def explodes(term_list, scratch, *, client, portrait, wanted):
        if "bad" in term_list[0]:
            raise RuntimeError("provider exploded")
        scratch.mkdir(parents=True, exist_ok=True)
        path = scratch / "cand_00.mp4"
        path.write_bytes(b"x" * 60_000)
        return [footage.Clip(path, "pixabay", term_list[0], 1080, 1920, 5.0,
                             source_url=term_list[0])]

    monkeypatch.setattr(footage, "_collect_candidates", explodes)
    clips, missing = await footage.gather(
        [_beat("good"), _beat("bad"), _beat("good two")], tmp_path
    )
    assert len(clips) == 2 and missing == ["bad"]


@pytest.mark.asyncio
async def test_no_footage_at_all_is_the_one_terminal_footage_case(
    tmp_path, stub_candidates
):
    stub_candidates["pools"] = {}
    with pytest.raises(footage.NoFootage):
        await footage.gather([_beat("a"), _beat("b")], tmp_path)


# ── relevance ranking and duplicate rejection ────────────────────────

@pytest.mark.asyncio
async def test_the_best_ranked_candidate_wins_not_the_first(
    tmp_path, stub_candidates
):
    """Taking the first search result is what produced unrelated footage."""
    stub_candidates["pools"] = {
        "gold vending machine": [
            {"url": "wrong"}, {"url": "alsowrong"}, {"url": "right"},
        ]
    }
    stub_candidates["scores"] = {"a gold vending machine": [2, 3, 9]}
    clips, _ = await footage.gather(
        [_beat("a gold vending machine", "gold vending machine")], tmp_path
    )
    assert len(clips) == 1
    assert clips[0].source_url == "right"


@pytest.mark.asyncio
async def test_a_beat_whose_candidates_are_all_unrelated_is_left_uncovered(
    tmp_path, stub_candidates
):
    """Better an uncovered beat than footage of the wrong thing."""
    stub_candidates["pools"] = {
        "ok": [{"url": "good"}],
        "x": [{"url": "a"}, {"url": "b"}],
    }
    stub_candidates["scores"] = {"an impossible shot": [1, 2]}
    clips, missing = await footage.gather(
        [_beat("a usable shot", "ok"), _beat("an impossible shot", "x")], tmp_path
    )
    assert len(clips) == 1, "an unrelated candidate was used anyway"
    assert missing == ["an impossible shot"]


@pytest.mark.asyncio
async def test_a_rejected_beat_gets_one_broader_second_attempt(
    tmp_path, stub_candidates
):
    """The narrow query is usually what failed, not the subject.

    A real Arabic run lost two beats this way: every candidate for
    "coral bleaching underwater" was rejected and the beat was abandoned
    without anyone ever searching "bleaching underwater".
    """
    stub_candidates["pools"] = {
        "coral bleaching underwater": [{"url": "bad-a"}, {"url": "bad-b"}],
        "bleaching underwater": [{"url": "wider-a"}, {"url": "wider-b"}],
    }
    stub_candidates["scores"] = {
        "a bleached reef": [[1, 2], [9, 3]],      # all rejected, then a good one
    }
    clips, missing = await footage.gather(
        [_beat("a bleached reef", "coral bleaching underwater")], tmp_path
    )
    assert missing == [], "the beat was abandoned without a second attempt"
    assert clips[0].source_url == "wider-a"
    assert ["bleaching underwater"] in stub_candidates["queries"]


@pytest.mark.asyncio
async def test_the_second_attempt_happens_at_most_once(tmp_path, stub_candidates):
    """Bounded on purpose: a good pick, not an unbounded search."""
    stub_candidates["pools"] = {
        "healthy reef": [{"url": "good"}],
        "coral bleaching underwater": [{"url": "bad-a"}, {"url": "bad-b"}],
        "bleaching underwater": [{"url": "still-bad-a"}, {"url": "still-bad-b"}],
        "underwater": [{"url": "never-reached"}],
    }
    stub_candidates["scores"] = {"a bleached reef": [0, 0]}
    clips, missing = await footage.gather(
        [
            _beat("a living reef", "healthy reef"),
            _beat("a bleached reef", "coral bleaching underwater"),
        ],
        tmp_path,
    )
    assert len(clips) == 1
    assert missing == ["a bleached reef"]
    assert ["underwater"] not in stub_candidates["queries"]


@pytest.mark.asyncio
async def test_runner_up_visuals_are_banked_for_the_extra_slots(
    tmp_path, stub_candidates
):
    """A 60-second video needs ~17 visuals and has ~10 beats.

    The other seven used to be the same clips shown again. A runner-up that
    already passed the ranker and the duplicate guard is a better answer and
    costs nothing extra -- it is already on disk.
    """
    stub_candidates["pools"] = {
        "one": [{"url": "a1"}, {"url": "a2"}, {"url": "a3"}],
        "two": [{"url": "b1"}, {"url": "b2"}],
    }
    reserves: list = []
    clips, _ = await footage.gather(
        [_beat("first", "one"), _beat("second", "two")], tmp_path,
        reserves=reserves,
    )
    assert [c.source_url for c in clips] == ["a1", "b1"]
    assert [c.source_url for c in reserves] == ["a2", "b2"]
    # A reserve is a distinct source, never a second copy of a chosen one.
    assert not {c.source_url for c in reserves} & {c.source_url for c in clips}


@pytest.mark.asyncio
async def test_banking_reserves_is_optional(tmp_path, stub_candidates):
    """Callers that do not ask for them get the original behaviour."""
    stub_candidates["pools"] = {"one": [{"url": "a1"}, {"url": "a2"}]}
    clips, _ = await footage.gather([_beat("first", "one")], tmp_path)
    assert [c.source_url for c in clips] == ["a1"]


@pytest.mark.asyncio
async def test_a_beat_stock_video_cannot_cover_falls_back_to_a_related_concept(
    tmp_path, stub_candidates
):
    """Rung 2. "A satellite view of the reef" -> "an aerial shot of the reef".

    Both show the viewer the same idea; only one of them exists on a free
    stock library.
    """
    from aivideo.fallback import Alternative

    stub_candidates["pools"] = {
        "healthy reef": [{"url": "good"}],
        "satellite view reef": [],
        "aerial reef drone": [{"url": "aerial"}],
    }
    stub_candidates["alternatives"] = {
        0: Alternative(
            concept="an aerial drone shot looking down on a reef",
            queries=["aerial reef drone"],
            evidentiary=True,
        )
    }
    clips, missing = await footage.gather(
        [
            _beat("a living reef", "healthy reef"),
            _beat("the reef from space", "satellite view reef"),
        ],
        tmp_path,
    )
    assert missing == []
    assert any(c.source_url == "aerial" for c in clips)


@pytest.mark.asyncio
async def test_a_still_with_motion_covers_a_beat_no_video_could(
    tmp_path, stub_candidates
):
    """Rungs 3 and 4. A photograph with a slow push is a shot, not a gap."""
    from aivideo.fallback import Alternative

    stub_candidates["pools"] = {
        "healthy reef": [{"url": "good"}],
        "polyp cutaway animation": [],
        "coral polyp macro": [],
    }
    stub_candidates["alternatives"] = {
        0: Alternative(
            concept="an extreme close-up of a living coral polyp",
            queries=["coral polyp macro"],
            still_queries=["coral polyp macro"],
            evidentiary=False,
        )
    }
    stub_candidates["stills"] = {
        "coral polyp macro": [{"provider": "pexels", "page": "photo-1"}],
    }
    clips, missing = await footage.gather(
        [
            _beat("a living reef", "healthy reef"),
            _beat("a polyp cutaway", "polyp cutaway animation"),
        ],
        tmp_path,
    )
    assert missing == []
    still = [c for c in clips if c.source_url == "photo-1"]
    assert still, "the still never became a clip"
    # Its own window, not one re-derived by shot detection: a slow push over
    # a photograph has no cuts and its whole length is the shot.
    assert still[0].window == (0.0, 5.0)


@pytest.mark.asyncio
async def test_a_still_that_is_not_about_the_beat_is_still_rejected(
    tmp_path, stub_candidates
):
    """The ladder lowers where we look, never the relevance bar."""
    from aivideo.fallback import Alternative

    stub_candidates["pools"] = {
        "healthy reef": [{"url": "good"}],
        "polyp cutaway animation": [],
        "coral polyp macro": [],
    }
    stub_candidates["alternatives"] = {
        0: Alternative(
            concept="an extreme close-up of a living coral polyp",
            still_queries=["coral polyp macro"],
            evidentiary=True,
        )
    }
    stub_candidates["stills"] = {
        "coral polyp macro": [{"provider": "pexels", "page": "photo-1"}],
    }
    stub_candidates["scores"] = {"a polyp cutaway": [1]}
    clips, missing = await footage.gather(
        [
            _beat("a living reef", "healthy reef"),
            _beat("a polyp cutaway", "polyp cutaway animation"),
        ],
        tmp_path,
    )
    assert missing == ["a polyp cutaway"]
    assert len(clips) == 1


@pytest.mark.asyncio
async def test_only_a_non_evidentiary_concept_may_be_generated(
    tmp_path, stub_candidates
):
    """Generating a named place or a real event would be inventing evidence."""
    from aivideo.fallback import Alternative

    stub_candidates["pools"] = {"healthy reef": [{"url": "good"}], "x y z": []}
    stub_candidates["allow_generated"] = True
    stub_candidates["alternatives"] = {
        0: Alternative(concept="the Great Barrier Reef from orbit",
                       queries=["x y z"], evidentiary=True),
    }
    _, missing = await footage.gather(
        [_beat("a living reef", "healthy reef"), _beat("from orbit", "x y z")],
        tmp_path,
    )
    assert missing == ["from orbit"]
    assert not stub_candidates.get("generated"), "an evidentiary beat was generated"

    stub_candidates["alternatives"] = {
        0: Alternative(concept="sunlight moving through shallow water",
                       queries=["x y z"], evidentiary=False),
    }
    _, missing = await footage.gather(
        [_beat("a living reef", "healthy reef"), _beat("from orbit", "x y z")],
        tmp_path / "second",
    )
    assert missing == []
    assert stub_candidates["generated"] == ["sunlight moving through shallow water"]


@pytest.mark.asyncio
async def test_the_same_source_is_never_used_for_two_beats(
    tmp_path, stub_candidates
):
    stub_candidates["pools"] = {
        "one": [{"url": "shared"}],
        "two": [{"url": "shared"}, {"url": "distinct"}],
    }
    clips, _ = await footage.gather([_beat("one"), _beat("two")], tmp_path)
    urls = [c.source_url for c in clips]
    assert len(urls) == len(set(urls)), f"a source repeated: {urls}"
    assert "distinct" in urls


@pytest.mark.asyncio
async def test_visually_near_identical_clips_are_rejected(
    tmp_path, stub_candidates
):
    """The same drone shot sold under two ids is the repeat viewers notice."""
    stub_candidates["pools"] = {
        "one": [{"url": "a"}],
        "two": [{"url": "b"}, {"url": "c"}],
    }
    # Beats 0 and 1's first candidate hash identically; the second differs.
    stub_candidates["hashes"] = {"beat_00": 0b1010, "beat_01": 0b1010}
    clips, missing = await footage.gather([_beat("one"), _beat("two")], tmp_path)
    # The duplicate is refused, so beat two ends up uncovered rather than
    # showing the same footage again.
    assert len(clips) == 1
    assert missing == ["two"]


@pytest.mark.asyncio
async def test_the_chosen_clip_is_cut_at_a_shot_boundary(tmp_path, stub_candidates, monkeypatch):
    from medialab import shots

    monkeypatch.setattr(
        shots, "longest_clean_window", lambda v, *, wanted, shots=None: (2.5, 3.0)
    )
    stub_candidates["pools"] = {"one": [{"url": "a"}]}
    clips, _ = await footage.gather([_beat("one")], tmp_path)
    assert clips[0].window == (2.5, 3.0)


def test_provider_order_puts_the_free_libraries_first():
    assert [name for name, _ in footage.PROVIDERS] == ["pexels", "pixabay"]


# ── voice ────────────────────────────────────────────────────────────

def test_missing_word_boundaries_fall_back_to_weighted_timings():
    words = voice._even_timings("a extraordinary word", 3.0)
    assert len(words) == 3
    assert words[0].start == 0.0
    assert words[-1].end == pytest.approx(3.0, rel=1e-6)
    # Longer words hold the screen longer.
    assert (words[1].end - words[1].start) > (words[0].end - words[0].start)


def test_edge_rate_and_volume_use_signed_percentages():
    assert voice._rate_arg(1.0) == "+0%"
    assert voice._rate_arg(1.25) == "+25%"
    assert voice._rate_arg(0.8) == "-20%"


# ── script ───────────────────────────────────────────────────────────

def test_the_word_budget_tracks_the_requested_duration():
    assert script_stage.word_budget(30, "en") == 75
    assert script_stage.word_budget(60, "en") == 150
    # Arabic is read more slowly, so the same seconds buy fewer words.
    assert script_stage.word_budget(60, "ar") < script_stage.word_budget(60, "en")


def test_the_prompt_demands_english_search_terms_for_an_arabic_script():
    prompt = script_stage.build_prompt("حقائق عن دبي", 30, "ar")
    assert "Every word of \"script\" must be Arabic" in prompt
    assert "in ENGLISH even when the script is Arabic" in prompt


def test_script_cleaning_removes_the_scaffolding_models_add():
    cleaned = script_stage._clean_script(
        "```\n## Intro\nNarration: **Dubai** is a city.\n[pause]\n- built fast\n```"
    )
    assert "##" not in cleaned and "**" not in cleaned
    assert "[pause]" not in cleaned and not cleaned.startswith("Narration")
    assert "Dubai is a city." in cleaned


def test_terms_always_come_back_even_from_an_empty_reply():
    terms = script_stage._clean_terms([], "Why the Titanic sank")
    assert terms and all(t.strip() for t in terms)


# ── the lifecycle, end to end on fixtures ────────────────────────────

@pytest.fixture
def fake_engine(monkeypatch, tmp_path):
    """Stand in for every external call and for FFmpeg."""
    calls = {"script": 0, "footage": 0, "voice": 0, "render": 0}

    async def fake_script(topic, *, duration_seconds, language, ledger=None):
        calls["script"] += 1
        if ledger is not None:
            ledger.record_model_call(
                model="gemini-3-flash-preview",
                input_tokens=400, output_tokens=120, label="script",
            )
        return "Dubai was built in a hurry.", [
            script_stage.Beat(
                says="Dubai was built in a hurry.",
                shows="a desert city skyline under construction",
                terms=["dubai skyline", "construction cranes"],
            ),
            script_stage.Beat(
                says="Roads crossed empty sand.",
                shows="an empty road through desert dunes",
                terms=["desert road", "sand dunes"],
            ),
        ]

    async def fake_gather(
        beats, directory, *, portrait=True, concurrency=3, rank=True,
        ledger=None, reserves=None,
    ):
        calls["footage"] += 1
        directory.mkdir(parents=True, exist_ok=True)
        clips = []
        for i, beat in enumerate(beats):
            p = directory / f"clip_{i:02d}.mp4"
            p.write_bytes(b"x" * 60_000)
            term = getattr(beat, "terms", [""])[0] if hasattr(beat, "terms") else str(beat)
            clip = footage.Clip(p, "pexels", term, 1080, 1920, 6.0)
            clip.window = (0.5, 4.0)
            clips.append(clip)
        return clips, []

    async def fake_voice(text, voice_id, output, *, rate=1.0, volume=1.0):
        calls["voice"] += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"x" * 40_000)
        words = [voice.Word(w, i * 0.5, i * 0.5 + 0.5)
                 for i, w in enumerate(text.split())]
        return voice.Narration(output, words, 12.0, voice_id)

    def fake_normalise(source, target, spec, seconds, start=0.0):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 20_000)
        return target

    def fake_track(clips, target, total, spec):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 20_000)
        return target

    def fake_render(*, visual, narration, output, spec, captions, music, duration):
        calls["render"] += 1
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"x" * 200_000)
        return output

    monkeypatch.setattr(pipeline.script_stage, "write_script", fake_script)
    monkeypatch.setattr(pipeline.footage, "gather", fake_gather)
    monkeypatch.setattr(pipeline.voice, "synthesize", fake_voice)
    monkeypatch.setattr(pipeline.compose, "normalise_clip", fake_normalise)
    monkeypatch.setattr(pipeline.compose, "build_visual_track", fake_track)
    monkeypatch.setattr(pipeline.compose, "render", fake_render)
    monkeypatch.setattr(pipeline.compose, "validate_output",
                        lambda p, **k: (True, "12.0s 1080x1920"))
    monkeypatch.setattr(pipeline.compose, "poster_frame", lambda v, t, at=1.0: None)
    return calls


SPEC = {"topic": "5 crazy facts about Dubai", "language": "en",
        "duration_seconds": 30, "aspect_ratio": "9:16"}


@pytest.mark.asyncio
async def test_a_full_run_produces_a_validated_video(tmp_path, fake_engine):
    seen: list[tuple[str, int]] = []
    state = await pipeline.run(SPEC, tmp_path, on_progress=lambda l, p: seen.append((l, p)))

    assert state.completed == list(pipeline.STAGES)
    assert Path(state.output).exists()
    assert state.providers_used == ["pexels"]
    # The customer saw plain-language progress, in order, and nothing internal.
    labels = [l for l, _ in seen]
    assert "Writing script" in labels and "Rendering" in labels
    for label in labels:
        assert not any(bad in label.lower() for bad in
                       ("pexels", "edge", "ffmpeg", "http", "error", "stage"))


@pytest.mark.asyncio
async def test_resuming_never_repeats_a_finished_stage(tmp_path, fake_engine):
    await pipeline.run(SPEC, tmp_path)
    assert fake_engine == {"script": 1, "footage": 1, "voice": 1, "render": 1}

    await pipeline.run(SPEC, tmp_path)          # a worker restart
    assert fake_engine == {"script": 1, "footage": 1, "voice": 1, "render": 1}, \
        "a completed stage was paid for twice"


@pytest.mark.asyncio
async def test_a_restart_mid_run_keeps_the_script_and_footage(tmp_path, fake_engine):
    """The two expensive stages must survive a crash in a later one."""
    state = pipeline.load_state(tmp_path)
    assert state.completed == []

    # Simulate: script + footage done, then the worker died.
    await pipeline.run(SPEC, tmp_path)
    state = pipeline.load_state(tmp_path)
    state.completed = ["script", "footage"]
    pipeline.save_state(tmp_path, state)
    before = dict(fake_engine)

    await pipeline.run(SPEC, tmp_path)
    assert fake_engine["script"] == before["script"], "script was rewritten"
    assert fake_engine["footage"] == before["footage"], "footage was re-downloaded"
    assert fake_engine["voice"] == before["voice"] + 1


@pytest.mark.asyncio
async def test_a_corrupt_checkpoint_restarts_instead_of_stranding_the_job(
    tmp_path, fake_engine
):
    (tmp_path / "state.json").write_text("{ not json", encoding="utf-8")
    state = await pipeline.run(SPEC, tmp_path)
    assert state.completed == list(pipeline.STAGES)


@pytest.mark.asyncio
async def test_an_empty_topic_is_terminal(tmp_path, fake_engine):
    with pytest.raises(pipeline.TerminalFailure):
        await pipeline.run({"topic": "   "}, tmp_path)


@pytest.mark.asyncio
async def test_no_footage_anywhere_is_terminal(tmp_path, fake_engine, monkeypatch):
    async def nothing(
        beats, directory, *, portrait=True, concurrency=3, rank=True,
        ledger=None, reserves=None,
    ):
        raise footage.NoFootage("nothing found")

    monkeypatch.setattr(pipeline.footage, "gather", nothing)
    with pytest.raises(pipeline.TerminalFailure):
        await pipeline.run(SPEC, tmp_path)


@pytest.mark.asyncio
async def test_a_transient_render_failure_is_recoverable_not_terminal(
    tmp_path, fake_engine, monkeypatch
):
    from aivideo import compose

    def boom(**kwargs):
        raise compose.RenderFailed("ffmpeg fell over")

    monkeypatch.setattr(pipeline.compose, "render", boom)
    with pytest.raises(pipeline.RecoverableFailure):
        await pipeline.run(SPEC, tmp_path)

    # And the checkpoint still holds everything that succeeded.
    state = pipeline.load_state(tmp_path)
    assert "script" in state.completed and "footage" in state.completed
    assert "render" not in state.completed


@pytest.mark.asyncio
async def test_music_failure_falls_back_to_a_silent_render(
    tmp_path, fake_engine, monkeypatch
):
    """Losing optional music must never lose the video."""
    from aivideo import compose

    attempts = {"n": 0}

    def render(*, visual, narration, output, spec, captions, music, duration):
        attempts["n"] += 1
        if music is not None:
            raise compose.RenderFailed("bad music stream")
        output.write_bytes(b"x" * 200_000)
        return output

    library = tmp_path / "music"
    library.mkdir()
    (library / "bed.mp3").write_bytes(b"x" * 1000)
    monkeypatch.setattr(pipeline.compose, "render", render)

    state = await pipeline.run(SPEC, tmp_path, music_library=library)
    assert attempts["n"] == 2
    assert any("without background music" in f for f in state.fallbacks)
    assert Path(state.output).exists()


@pytest.mark.asyncio
async def test_an_unpreparable_clip_is_dropped_not_fatal(
    tmp_path, fake_engine, monkeypatch
):
    def one_bad(source, target, spec, seconds, start=0.0):
        if source.name.endswith("_00.mp4"):
            raise RuntimeError("corrupt download")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x" * 20_000)
        return target

    monkeypatch.setattr(pipeline.compose, "normalise_clip", one_bad)
    state = await pipeline.run(SPEC, tmp_path)
    assert Path(state.output).exists()
    assert any("replaced an unusable clip" in f for f in state.fallbacks)


@pytest.mark.asyncio
async def test_a_video_that_fails_validation_reopens_render(
    tmp_path, fake_engine, monkeypatch
):
    """Never ship a file that is not a video, even if render claimed success."""
    monkeypatch.setattr(pipeline.compose, "validate_output",
                        lambda p, **k: (False, "output has no audio stream"))
    with pytest.raises(pipeline.RecoverableFailure):
        await pipeline.run(SPEC, tmp_path)
    assert "render" not in pipeline.load_state(tmp_path).completed


@pytest.mark.asyncio
async def test_a_full_disk_stops_before_rendering(tmp_path, fake_engine, monkeypatch):
    monkeypatch.setattr(pipeline, "free_bytes", lambda p: 10 * 1024 * 1024)
    with pytest.raises(pipeline.RecoverableFailure, match="disk space"):
        await pipeline.run(SPEC, tmp_path)


@pytest.mark.asyncio
async def test_arabic_runs_end_to_end(tmp_path, fake_engine):
    state = await pipeline.run(
        {"topic": "حقائق عن دبي", "language": "ar", "duration_seconds": 30},
        tmp_path,
    )
    assert state.completed == list(pipeline.STAGES)


# ── cost ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_finished_run_records_what_it_cost(tmp_path, fake_engine):
    state = await pipeline.run(SPEC, tmp_path)
    assert state.cost_usd > 0, "a metered model call was not costed"
    assert state.cost_usd < 0.05, "a 30s video should cost fractions of a cent"
    labels = [i["label"] for i in state.cost_items]
    assert "script" in labels
    # The free stages are itemised too, so the total is explainable.
    assert "voice" in labels


@pytest.mark.asyncio
async def test_resuming_does_not_double_charge(tmp_path, fake_engine):
    first = await pipeline.run(SPEC, tmp_path)
    again = await pipeline.run(SPEC, tmp_path)
    assert again.cost_usd == first.cost_usd


def test_the_ledger_prices_a_known_model_and_flags_an_unknown_one():
    from aivideo.cost import CostLedger

    ledger = CostLedger()
    ledger.record_model_call(
        model="gemini-3-flash-preview", input_tokens=1_000_000,
        output_tokens=0, label="script",
    )
    assert ledger.total_usd == pytest.approx(0.30)
    assert ledger.unpriced_models == []

    ledger.record_model_call(
        model="some-new-model", input_tokens=100, output_tokens=10, label="x"
    )
    assert "some-new-model" in ledger.unpriced_models, \
        "an unpriced model must be visible, not silently free"


def test_free_stages_are_recorded_as_free_not_omitted():
    from aivideo.cost import CostLedger

    ledger = CostLedger()
    ledger.record_free("voice", "edge")
    assert ledger.total_usd == 0.0
    assert ledger.items[0]["detail"] == "edge"


# ── music selection ──────────────────────────────────────────────────

def test_auto_music_is_deterministic_so_a_resume_sounds_the_same(tmp_path):
    library = tmp_path / "m"
    library.mkdir()
    for name in ("a.mp3", "b.mp3", "c.mp3"):
        (library / name).write_bytes(b"x")
    spec = normalise_spec({"topic": "Dubai", "music": "auto"})
    picked = pipeline.pick_music(spec, library)
    assert picked is not None
    assert pipeline.pick_music(spec, library) == picked


def test_no_music_library_is_not_an_error(tmp_path):
    spec = normalise_spec({"topic": "t", "music": "auto"})
    assert pipeline.pick_music(spec, None) is None
    assert pipeline.pick_music(spec, tmp_path / "missing") is None


def test_music_none_means_none(tmp_path):
    library = tmp_path / "m"
    library.mkdir()
    (library / "a.mp3").write_bytes(b"x")
    spec = normalise_spec({"topic": "t", "music": "none"})
    assert pipeline.pick_music(spec, library) is None


# ── isolation from the other products ────────────────────────────────

def test_the_engine_does_not_import_the_channel_pipeline():
    """A broken Football module must not be able to break this product."""
    import inspect

    for module in (pipeline, footage, subtitles, voice, script_stage):
        source = inspect.getsource(module)
        assert "from core" not in source, f"{module.__name__} imports core/"
        assert "import core" not in source, f"{module.__name__} imports core/"


def test_output_validation_rejects_the_shapes_that_are_not_videos(tmp_path):
    from aivideo import compose

    missing = tmp_path / "nope.mp4"
    ok, why = compose.validate_output(missing)
    assert not ok and "no output file" in why

    tiny = tmp_path / "tiny.mp4"
    tiny.write_bytes(b"x" * 100)
    ok, why = compose.validate_output(tiny)
    assert not ok and "bytes" in why
