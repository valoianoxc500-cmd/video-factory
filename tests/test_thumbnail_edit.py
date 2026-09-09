"""Thumbnails are edited from a real photograph, on one provider.

Two things are load-bearing. First, the choice of base image: an edit that
starts from a generated picture of a footballer produces a face that is not
his, which is the most visible way a thumbnail can be wrong. Second, that this
path stays separate from scene visuals and stays on one provider.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from PIL import Image

from core import thumbnail_source
from core.providers.thumbnails import FalGeminiFlashEditProvider, _data_uri
from core.utils import Script, ScriptSection, VisualSlot


def _slot(index: int, keywords: str = "") -> VisualSlot:
    return VisualSlot(
        visual="info_slide",
        prompt="a photograph",
        keywords=keywords,
        props={},
    )


def _script(*sections) -> Script:
    return Script(
        title="t",
        video_type="explainer",
        thumbnail_text="X",
        thumbnail_brief="b",
        thumbnail_strategy="s",
        sections=list(sections),
    )


def _section(section_id: int, *slots) -> ScriptSection:
    return ScriptSection(id=section_id, narration="n", slots=list(slots))


def _image(path, size=(1200, 800)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color=(20, 20, 20)).save(path)
    return path


def _workspace(tmp_path, files: dict, provenance: list | None = None):
    """A workspace with raw images and an optional provenance record."""
    for name, size in files.items():
        _image(tmp_path / "images" / "raw" / name, size)
    if provenance is not None:
        (tmp_path / "asset_provenance.json").write_text(
            json.dumps({"assets": provenance}), encoding="utf-8"
        )
    return tmp_path


# ── choosing the base ────────────────────────────────────────────────

def test_a_real_photograph_beats_a_generated_one_even_when_smaller(tmp_path):
    ws = _workspace(
        tmp_path,
        {"section_001_01.png": (600, 400), "section_002_01.png": (2000, 1400)},
        provenance=[
            {"file": "section_001_01.png", "platform": "pexels"},
            {"file": "section_002_01.png", "generated": True, "platform": "gemini"},
        ],
    )
    script = _script(_section(1, _slot(1)), _section(2, _slot(1)))
    picked = thumbnail_source.pick_source_images(ws, script, [])
    assert picked[0][0].name == "section_001_01.png"
    assert picked[0][1]["real_photograph"] is True


def test_an_image_naming_a_story_subject_wins_among_real_photographs(tmp_path):
    ws = _workspace(
        tmp_path,
        {"section_001_01.png": (2000, 1400), "section_002_01.png": (900, 600)},
        provenance=[
            {"file": "section_001_01.png", "platform": "pexels"},
            {"file": "section_002_01.png", "platform": "pexels"},
        ],
    )
    script = _script(
        _section(1, _slot(1, "stadium wide shot")),
        _section(2, _slot(1, "Emiliano Martinez goalkeeper")),
    )
    picked = thumbnail_source.pick_source_images(ws, script, ["Martinez"])
    # Smaller, but it is the one with the player in it.
    assert picked[0][0].name == "section_002_01.png"
    assert picked[0][1]["names_a_story_subject"] is True


def test_the_largest_wins_when_nothing_else_separates_them(tmp_path):
    ws = _workspace(
        tmp_path,
        {"section_001_01.png": (800, 600), "section_002_01.png": (2000, 1400)},
        provenance=[
            {"file": "section_001_01.png", "platform": "pexels"},
            {"file": "section_002_01.png", "platform": "pexels"},
        ],
    )
    script = _script(_section(1, _slot(1)), _section(2, _slot(1)))
    picked = thumbnail_source.pick_source_images(ws, script, [])
    assert picked[0][0].name == "section_002_01.png"


def test_tiny_images_are_never_a_base(tmp_path):
    ws = _workspace(tmp_path, {"section_001_01.png": (80, 60)})
    assert thumbnail_source.pick_source_images(ws, _script(_section(1, _slot(1))), []) == []


def test_files_that_are_not_beat_assets_are_ignored(tmp_path):
    ws = _workspace(tmp_path, {"section_001_01.png": (1200, 800)})
    _image(ws / "images" / "raw" / "watermark.png", (1200, 800))
    picked = thumbnail_source.pick_source_images(ws, _script(_section(1, _slot(1))), [])
    assert [p.name for p, _ in picked] == ["section_001_01.png"]


def test_an_empty_workspace_yields_no_base(tmp_path):
    assert thumbnail_source.pick_source_images(tmp_path, _script(), []) == []


def test_missing_provenance_still_ranks_rather_than_failing(tmp_path):
    """A workspace from before provenance existed must still produce a base."""
    ws = _workspace(tmp_path, {"section_001_01.png": (1200, 800)})
    picked = thumbnail_source.pick_source_images(ws, _script(_section(1, _slot(1))), [])
    assert picked and picked[0][1]["platform"] == "unknown"


def test_unreadable_provenance_is_survived(tmp_path):
    ws = _workspace(tmp_path, {"section_001_01.png": (1200, 800)})
    (ws / "asset_provenance.json").write_text("{not json", encoding="utf-8")
    assert thumbnail_source.pick_source_images(ws, _script(_section(1, _slot(1))), [])


# ── the trophy regression ────────────────────────────────────────────
#
# The exact run that failed: Real Madrid v Inter, sixteen sourced photographs,
# every one real, every one 1920x1080 after processing, every one naming a
# story subject. Ranking on "real, largest, first" chose the trophy, the edit
# had no face to preserve, and it invented two players.

#: Beat keywords from that run, in the order the directory listed them.
_REAL_V_INTER = [
    ("section_001_01", "UEFA Champions League trophy close up"),
    ("section_001_02", "Real Madrid team celebration 2024"),
    ("section_001_03", "Carlo Ancelotti Real Madrid coaching"),
    ("section_001_04", "Simone Inzaghi Inter Milan sideline"),
    ("section_001_05", "Real Madrid fans flags stadium"),
    ("section_001_06", "Champions League match ball pitch"),
    ("section_001_07", "Santiago Bernabeu stadium night exterior"),
    ("section_002_01", "Jude Bellingham Real Madrid action"),
    ("section_002_02", "Lautaro Martinez Inter goal celebration"),
    ("section_002_03", "soccer football tactics board formation"),
    ("section_002_04", "Vinicius Junior Real Madrid dribble"),
    ("section_002_09", "San Siro stadium night photography"),
]

_SUBJECTS = ["UEFA", "Champions", "Real", "Madrid", "Carlo", "Ancelotti",
             "Simone", "Inzaghi", "Inter", "Milan"]


def _real_v_inter(tmp_path):
    """That workspace: identical dimensions, all real, all on subject."""
    sections: dict[int, list] = {}
    for stem, keywords in _REAL_V_INTER:
        section_id = int(stem.split("_")[1])
        sections.setdefault(section_id, []).append(_slot(1, keywords))
        _image(tmp_path / "images" / "raw" / f"{stem}.jpg", (1920, 1080))
    (tmp_path / "asset_provenance.json").write_text(
        json.dumps({"assets": [
            {"file": f"{stem}.jpg", "platform": "serper"}
            for stem, _ in _REAL_V_INTER
        ]}),
        encoding="utf-8",
    )
    script = _script(*[
        _section(sid, *slots) for sid, slots in sorted(sections.items())
    ])
    return tmp_path, script


def test_a_player_photo_is_chosen_over_the_champions_league_trophy(tmp_path):
    """The regression. The trophy must not win."""
    ws, script = _real_v_inter(tmp_path)
    picked = thumbnail_source.pick_source_images(ws, script, _SUBJECTS)
    assert picked, "a base should have been found"
    chosen, why = picked[0]
    assert chosen.stem != "section_001_01", "the trophy was chosen again"
    assert why["people"], f"chose {why['file']} which names nobody"
    assert why["depicts_person"] is True


def test_the_chosen_base_is_one_of_the_named_footballers(tmp_path):
    ws, script = _real_v_inter(tmp_path)
    chosen, why = thumbnail_source.pick_source_images(ws, script, _SUBJECTS)[0]
    assert chosen.stem in {
        "section_001_03",  # Carlo Ancelotti
        "section_001_04",  # Simone Inzaghi
        "section_002_01",  # Jude Bellingham
        "section_002_02",  # Lautaro Martinez
        "section_002_04",  # Vinicius Junior
    }, why


def test_objects_and_venues_rank_below_every_person(tmp_path):
    """Trophy, ball, tactics board and both stadiums go to the bottom."""
    ws, script = _real_v_inter(tmp_path)
    order = [p.stem for p, _ in thumbnail_source.rank_source_images(ws, script, _SUBJECTS)]
    objects = {"section_001_01", "section_001_06", "section_001_07",
               "section_002_03", "section_002_09"}
    people = {"section_001_03", "section_001_04", "section_002_01",
              "section_002_02", "section_002_04"}
    assert max(order.index(s) for s in people) < min(order.index(s) for s in objects)


def test_a_stadium_named_after_a_person_is_still_a_stadium(tmp_path):
    """"Santiago Bernabeu" must not read as a footballer."""
    assert thumbnail_source.named_people(
        "Santiago Bernabeu stadium night exterior"
    ) == []
    assert thumbnail_source.named_people("San Siro stadium night photography") == []


def test_a_competition_is_not_a_person(tmp_path):
    assert thumbnail_source.named_people("UEFA Champions League trophy close up") == []


def test_real_footballers_are_recognised_as_people():
    assert thumbnail_source.named_people("Jude Bellingham Real Madrid action") == [
        "Jude Bellingham"
    ]
    assert thumbnail_source.named_people("Carlo Ancelotti Real Madrid coaching") == [
        "Carlo Ancelotti"
    ]


def test_a_crowd_counts_as_people_even_with_nobody_named():
    assert thumbnail_source.shows_people("Real Madrid fans flags stadium")
    assert thumbnail_source.shows_people("Inter Milan fans Curva Nord cheering")
    assert not thumbnail_source.shows_people("San Siro stadium night photography")


def test_a_generated_player_never_outranks_a_real_object(tmp_path):
    """Real photography stays the highest preference, as specified."""
    ws = _workspace(
        tmp_path,
        {"section_001_01.jpg": (1920, 1080), "section_001_02.jpg": (1920, 1080)},
        provenance=[
            {"file": "section_001_01.jpg", "platform": "serper"},
            {"file": "section_001_02.jpg", "generated": True, "platform": "gemini"},
        ],
    )
    script = _script(
        _section(1,
                 _slot(1, "UEFA Champions League trophy close up"),
                 _slot(2, "Jude Bellingham Real Madrid action")),
    )
    chosen, why = thumbnail_source.pick_source_images(ws, script, _SUBJECTS)[0]
    assert chosen.stem == "section_001_01"
    assert why["real_photograph"] is True


def test_selection_is_deterministic_and_not_directory_order(tmp_path):
    """Identical scores must resolve the same way every time, by content."""
    ws, script = _real_v_inter(tmp_path)
    runs = {
        tuple(p.stem for p, _ in thumbnail_source.rank_source_images(
            ws, script, _SUBJECTS))
        for _ in range(4)
    }
    assert len(runs) == 1, "ranking is not deterministic"


def test_identical_people_photos_are_separated_by_shape_not_by_name(tmp_path):
    """Two equal player shots: the one nearer 16:9 wins, whatever it is called."""
    raw = tmp_path / "images" / "raw"
    _image(raw / "section_001_01.jpg", (1000, 1000))     # square, crops badly
    _image(raw / "section_002_01.jpg", (1920, 1080))     # already 16:9
    (tmp_path / "asset_provenance.json").write_text(
        json.dumps({"assets": [
            {"file": "section_001_01.jpg", "platform": "serper"},
            {"file": "section_002_01.jpg", "platform": "serper"},
        ]}), encoding="utf-8")
    script = _script(
        _section(1, _slot(1, "Jude Bellingham Real Madrid action")),
        _section(2, _slot(1, "Lautaro Martinez Inter goal celebration")),
    )
    chosen, _ = thumbnail_source.pick_source_images(tmp_path, script, _SUBJECTS)[0]
    assert chosen.stem == "section_002_01"


# ── a brief naming two people must supply two real faces ─────────────
#
# The second failure: with one real photo (the Inter coach) and a brief asking
# for Bellingham v Lautaro, the model kept the coach and invented a Real
# Madrid player to fill the other half. Ranking now follows the brief, and
# selection returns one photograph per person it names.

#: The real brief from that run, in Arabic, naming both players.
_ARABIC_BRIEF = (
    "صورة مقسومة. على اليسار، جود بيلينجهام بقميص ريال مدريد على خلفية بيضاء. "
    "على اليمين، لاوتارو مارتينيز بقميص إنتر ميلان على خلفية زرقاء وسوداء."
)
_ENGLISH_BRIEF = (
    "Split image. On the left, Jude Bellingham in a Real Madrid shirt. "
    "On the right, Lautaro Martinez in an Inter Milan shirt."
)


def test_the_brief_is_matched_across_scripts(tmp_path):
    """An Arabic brief must resolve to the English keywords photos carry."""
    assert thumbnail_source.brief_names("Jude Bellingham", _ARABIC_BRIEF)
    assert thumbnail_source.brief_names("Lautaro Martinez", _ARABIC_BRIEF)
    # People the run has photographs of but the brief does not ask for.
    assert not thumbnail_source.brief_names("Simone Inzaghi", _ARABIC_BRIEF)
    assert not thumbnail_source.brief_names("Carlo Ancelotti", _ARABIC_BRIEF)
    assert not thumbnail_source.brief_names("Thibaut Courtois", _ARABIC_BRIEF)


def test_the_brief_is_matched_in_english_too(tmp_path):
    assert thumbnail_source.brief_names("Jude Bellingham", _ENGLISH_BRIEF)
    assert thumbnail_source.brief_names("Lautaro Martinez", _ENGLISH_BRIEF)
    assert not thumbnail_source.brief_names("Simone Inzaghi", _ENGLISH_BRIEF)


@pytest.mark.parametrize("brief", [_ARABIC_BRIEF, _ENGLISH_BRIEF])
def test_bellingham_and_lautaro_are_both_selected(tmp_path, brief):
    """The regression: two named people, two real photographs, no invention."""
    ws, script = _real_v_inter(tmp_path)
    picked = thumbnail_source.pick_source_images(
        ws, script, _SUBJECTS, brief=brief
    )
    assert len(picked) == 2, [w["file"] for _, w in picked]

    chosen = {w["file"] for _, w in picked}
    assert chosen == {"section_002_01.jpg", "section_002_02.jpg"}, chosen

    named = {n for _, w in picked for n in w["brief_people"]}
    assert named == {"Jude Bellingham", "Lautaro Martinez"}


def test_the_coach_and_the_trophy_are_both_rejected_for_this_brief(tmp_path):
    """Inzaghi has a photo and is in the story, but the brief did not ask."""
    ws, script = _real_v_inter(tmp_path)
    picked = thumbnail_source.pick_source_images(
        ws, script, _SUBJECTS, brief=_ARABIC_BRIEF
    )
    files = {w["file"] for _, w in picked}
    assert "section_001_04.jpg" not in files, "Inzaghi chosen despite the brief"
    assert "section_001_01.jpg" not in files, "the trophy is back"


def test_one_photograph_per_person_never_two_of_the_same(tmp_path):
    """Two Bellingham shots are one subject; the second slot must go to Lautaro."""
    ws, script = _real_v_inter(tmp_path)
    # A second Bellingham photo, larger, so it would outrank on every
    # tiebreaker if identity were not tracked.
    _image(ws / "images" / "raw" / "section_003_01.jpg", (2400, 1350))
    script.sections.append(
        _section(3, _slot(1, "Jude Bellingham Real Madrid celebration"))
    )
    picked = thumbnail_source.pick_source_images(
        ws, script, _SUBJECTS, brief=_ARABIC_BRIEF
    )
    named = [n for _, w in picked for n in w["brief_people"]]
    assert sorted(named) == ["Jude Bellingham", "Lautaro Martinez"]
    assert len(picked) == 2


def test_a_brief_naming_nobody_we_have_falls_back_to_one_strong_base(tmp_path):
    ws, script = _real_v_inter(tmp_path)
    picked = thumbnail_source.pick_source_images(
        ws, script, _SUBJECTS, brief="Split image of two anonymous supporters."
    )
    assert len(picked) == 1
    assert picked[0][1]["depicts_person"] is True


def test_the_brief_outranks_slot_keywords(tmp_path):
    """A person in the brief beats a person merely in the scene keywords."""
    ws, script = _real_v_inter(tmp_path)
    ranked = thumbnail_source.rank_source_images(
        ws, script, _SUBJECTS, brief=_ARABIC_BRIEF
    )
    order = [w["file"] for _, w in ranked]
    assert order[0] in {"section_002_01.jpg", "section_002_02.jpg"}
    assert order[1] in {"section_002_01.jpg", "section_002_02.jpg"}
    # Inzaghi is a named person and a story subject, but not in the brief.
    assert order.index("section_001_04.jpg") > 1


def test_transliteration_skeleton_survives_spelling_differences():
    s = thumbnail_source._skeleton
    assert s("Bellingham") == s("belinjham") == s("Belingham")
    assert s("Martinez") == s("martiniz") == s("Martínez")
    assert s("Bellingham") != s("Martinez")


# ── the prompt may only claim what the base shows ────────────────────

def _prompt_for(monkeypatch, tmp_path, *, people, depicts):
    """The prompt _generate_ai_thumbnail actually sends."""
    from core import thumbnailer as tn

    seen = {}

    async def fake_edit(prompt, output_path, *, source_images, aspect_ratio="16:9",
                        operation_label=None):
        seen["prompt"] = prompt
        Image.new("RGB", (1280, 720), color=(0, 0, 0)).save(output_path)
        return output_path

    monkeypatch.setattr(tn.prompts, "thumbnail_generation_prompt",
                        lambda **kw: "BASE PROMPT")
    monkeypatch.setattr(tn.clients, "edit_thumbnail_image", fake_edit)

    from tests.test_thumbnailer import _config

    source = _image(tmp_path / "section_001_01.jpg")
    asyncio.run(tn._generate_ai_thumbnail(
        title="t", thumbnail_text="X", thumbnail_brief="b",
        strategy_instruction="s", content_context="c",
        config=_config(), output_path=tmp_path / "out.png",
        source_images=[source], subjects=_SUBJECTS,
        # One list of names per image, positionally aligned.
        source_people=people, depicts_person=depicts,
    ))
    return seen["prompt"]


def test_a_named_person_base_asks_for_that_face_to_be_preserved(monkeypatch, tmp_path):
    prompt = _prompt_for(monkeypatch, tmp_path,
                         people=[["Jude Bellingham"]], depicts=True)
    assert "Jude Bellingham" in prompt
    assert "Preserve every face" in prompt
    assert "Image 1: Jude Bellingham" in prompt


def test_an_object_base_never_claims_a_person_is_present(monkeypatch, tmp_path):
    """The bug: asserting a face over a trophy invited the model to draw one."""
    prompt = _prompt_for(monkeypatch, tmp_path, people=[], depicts=False)
    assert "OBJECT OR PLACE" in prompt
    assert "Do NOT add, draw or invent any recognisable person" in prompt
    assert "Preserve their face" not in prompt
    # And it must not name the story's people, which would be an instruction
    # to draw them.
    for subject in ("Ancelotti", "Inzaghi", "Bellingham"):
        assert subject not in prompt


def test_an_unnamed_crowd_base_preserves_without_naming_anyone(monkeypatch, tmp_path):
    prompt = _prompt_for(monkeypatch, tmp_path, people=[], depicts=True)
    assert "shows real people" in prompt
    assert "do not add any other identifiable person" in prompt.lower()


def test_two_named_people_are_mapped_to_their_own_photographs(monkeypatch, tmp_path):
    """Without a mapping the model may merge or swap the two faces."""
    prompt = _prompt_for(
        monkeypatch, tmp_path,
        people=[["Jude Bellingham"], ["Lautaro Martinez"]],
        depicts=True,
    )
    assert "Image 1: Jude Bellingham" in prompt
    assert "Image 2: Lautaro Martinez" in prompt
    assert "Do NOT add, draw or invent any additional person" in prompt
    assert "must come from a supplied photograph" in prompt


# ── the provider ─────────────────────────────────────────────────────

def test_provider_needs_a_key():
    status = FalGeminiFlashEditProvider(api_key="").status()
    assert not status.usable
    assert "FAL_KEY" in status.reason


def test_the_model_is_the_one_that_was_asked_for():
    provider = FalGeminiFlashEditProvider(api_key="k")
    assert provider.MODEL == "fal-ai/gemini-25-flash-image/edit"
    assert "gemini-25-flash-image/edit" in provider.URL


def test_editing_without_a_source_image_is_refused(tmp_path):
    """It is an edit model; a prompt alone would silently invent a face."""
    provider = FalGeminiFlashEditProvider(api_key="k")
    with pytest.raises(ValueError, match="edit model"):
        asyncio.run(provider.edit("p", [], client=None))


def test_a_source_path_that_does_not_exist_is_refused(tmp_path):
    provider = FalGeminiFlashEditProvider(api_key="k")
    with pytest.raises(ValueError, match="edit model"):
        asyncio.run(provider.edit("p", [tmp_path / "gone.png"], client=None))


def test_data_uri_carries_the_bytes_and_the_type(tmp_path):
    path = _image(tmp_path / "a.png")
    uri = _data_uri(path)
    assert uri.startswith("data:image/png;base64,")
    assert len(uri) > 100


class _Response:
    def __init__(self, payload=None, content=b"", status_code=200, text=""):
        self._payload = payload
        self.content = content
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Client:
    """Captures the one request, and serves the image URL it returns."""

    def __init__(self, payload):
        self.payload = payload
        self.body = None
        self.headers = None
        self.fetched = None

    async def post(self, url, *, headers=None, json=None):
        self.headers = headers
        self.body = json
        return _Response(payload=self.payload)

    async def get(self, url):
        self.fetched = url
        return _Response(content=b"PNGBYTES")


def test_the_request_sends_the_source_image_and_the_prompt(tmp_path):
    provider = FalGeminiFlashEditProvider(api_key="k")
    client = _Client({"images": [{"url": "https://cdn/x.png"}]})
    source = _image(tmp_path / "section_001_01.png")

    out = asyncio.run(provider.edit("make it dramatic", [source], client=client))

    assert out == b"PNGBYTES"
    assert client.body["prompt"] == "make it dramatic"
    assert len(client.body["image_urls"]) == 1
    assert client.body["image_urls"][0].startswith("data:image/png;base64,")
    assert client.headers["Authorization"] == "Key k"
    assert client.fetched == "https://cdn/x.png"


def test_an_inline_data_uri_result_is_decoded_rather_than_fetched(tmp_path):
    import base64

    provider = FalGeminiFlashEditProvider(api_key="k")
    encoded = base64.b64encode(b"INLINE").decode()
    client = _Client({"images": [{"url": f"data:image/png;base64,{encoded}"}]})
    out = asyncio.run(
        provider.edit("p", [_image(tmp_path / "a.png")], client=client)
    )
    assert out == b"INLINE"
    assert client.fetched is None


def test_no_image_in_the_response_raises(tmp_path):
    provider = FalGeminiFlashEditProvider(api_key="k")
    client = _Client({"images": [], "description": "refused"})
    with pytest.raises(Exception):
        asyncio.run(provider.edit("p", [_image(tmp_path / "a.png")], client=client))


def test_an_error_reports_the_body_not_just_the_status(tmp_path):
    """A bare 422 is unactionable: the reason is only ever in the body."""

    class _Failing(_Client):
        async def post(self, url, *, headers=None, json=None):
            return _Response(
                status_code=422,
                text='{"detail":[{"msg":"why it was refused"}]}',
            )

    provider = FalGeminiFlashEditProvider(api_key="k")
    with pytest.raises(RuntimeError, match="why it was refused"):
        asyncio.run(
            provider.edit("p", [_image(tmp_path / "a.png")], client=_Failing({}))
        )


def test_the_aspect_ratio_reaches_the_request(tmp_path):
    provider = FalGeminiFlashEditProvider(api_key="k")
    client = _Client({"images": [{"url": "https://cdn/x.png"}]})
    asyncio.run(
        provider.edit(
            "p", [_image(tmp_path / "a.png")], client=client, aspect_ratio="9:16"
        )
    )
    assert client.body["aspect_ratio"] == "9:16"


def test_the_trace_records_which_photograph_the_cover_was_built_from(tmp_path,
                                                                    monkeypatch):
    """Provenance for a thumbnail is its AI trace, so the base must be in it."""
    import clients

    written = {}
    monkeypatch.setattr(clients, "reserve_trace", lambda **kw: "trace-1")
    monkeypatch.setattr(clients, "_trace_suffix", lambda ref: "")
    monkeypatch.setattr(clients, "base_payload", lambda ref, **kw: {})
    monkeypatch.setattr(
        clients, "write_trace", lambda ref, payload: written.update(payload)
    )
    monkeypatch.setattr(clients.costs, "record_generate_content_cost", lambda **kw: None)

    source = _image(tmp_path / "section_001_01.png")

    class _Prov:
        name = "fal_gemini_flash_edit"
        MODEL = "fal-ai/gemini-25-flash-image/edit"
        TIMEOUT = 10.0

        def status(self):
            from core.providers.base import Availability, ProviderStatus

            return ProviderStatus(self.name, Availability.READY)

        async def edit(self, prompt, images, *, client, aspect_ratio="16:9"):
            return b"PNG"

    monkeypatch.setattr(
        "core.providers.thumbnails.FalGeminiFlashEditProvider", _Prov
    )

    out = asyncio.run(
        clients.edit_thumbnail_image.__wrapped__(
            "p", tmp_path / "thumb.png", source_images=[source]
        )
    )
    assert out is not None
    assert written["request"]["model"] == "fal-ai/gemini-25-flash-image/edit"
    assert str(source) in written["request"]["source_images"]


# ── the thumbnail call is priced ─────────────────────────────────────
#
# Before the catalog carried fal, every thumbnail was recorded as *unpriced*:
# honest, but it meant the single most expensive image in a run was invisible
# in cost reporting.

def _tracked(tmp_path):
    from core import costs

    costs.initialize_cost_tracking(
        workspace=tmp_path, run_id="pricing_test", channel="test"
    )
    return costs


def test_the_thumbnail_model_is_in_the_pricing_catalog():
    from core.costs import _load_pricing_catalog

    catalog = _load_pricing_catalog()
    entry = next(
        (e for e in catalog.entries
         if e.model == "fal-ai/gemini-25-flash-image/edit"),
        None,
    )
    assert entry is not None, "the thumbnail model has no pricing entry"
    assert entry.service == "generate_content_image"
    assert entry.output_image_rate_usd_per_image == 0.039
    assert entry.source_url.startswith("https://fal.ai/")


def test_one_thumbnail_is_priced_at_the_published_rate(tmp_path):
    from types import SimpleNamespace

    costs = _tracked(tmp_path)
    costs.record_generate_content_cost(
        model="fal-ai/gemini-25-flash-image/edit",
        usage_metadata=SimpleNamespace(),
        operation="thumbnail_generate",
        service="generate_content_image",
        provider="fal_ai",
        generated_images=1,
    )
    events = costs.get_tracker().events
    assert len(events) == 1
    assert events[0].estimated_usd == pytest.approx(0.039)
    assert events[0].generated_images == 1


def test_the_thumbnail_call_is_no_longer_unpriced(tmp_path):
    """The regression: it used to land in the missing-pricing report."""
    from types import SimpleNamespace

    costs = _tracked(tmp_path)
    costs.record_generate_content_cost(
        model="fal-ai/gemini-25-flash-image/edit",
        usage_metadata=SimpleNamespace(),
        operation="thumbnail_generate",
        service="generate_content_image",
        provider="fal_ai",
        generated_images=1,
    )
    event = costs.get_tracker().events[0]
    assert event.estimated_usd is not None, "still recorded as unpriced"
    assert "Pricing incomplete" not in " ".join(event.notes or [])


def test_fal_is_not_claimed_to_be_reconcilable_against_gcp_billing(tmp_path):
    """fal bills separately; its calls never appear in the GCP export."""
    from types import SimpleNamespace

    costs = _tracked(tmp_path)
    costs.record_generate_content_cost(
        model="fal-ai/gemini-25-flash-image/edit",
        usage_metadata=SimpleNamespace(),
        operation="thumbnail_generate",
        service="generate_content_image",
        provider="fal_ai",
        generated_images=1,
    )
    assert costs.get_tracker().events[0].reconciliation_supported is False


def test_the_real_call_path_prices_itself(tmp_path, monkeypatch):
    """End to end through clients.edit_thumbnail_image, with fal stubbed."""
    import clients

    costs = _tracked(tmp_path)
    monkeypatch.setattr(clients, "reserve_trace", lambda **kw: None)

    class _Prov:
        name = "fal_gemini_flash_edit"
        MODEL = "fal-ai/gemini-25-flash-image/edit"
        TIMEOUT = 10.0

        def status(self):
            from core.providers.base import Availability, ProviderStatus

            return ProviderStatus(self.name, Availability.READY)

        async def edit(self, prompt, images, *, client, aspect_ratio="16:9"):
            return b"PNG"

    monkeypatch.setattr("core.providers.thumbnails.FalGeminiFlashEditProvider", _Prov)

    out = asyncio.run(
        clients.edit_thumbnail_image.__wrapped__(
            "p", tmp_path / "t.png",
            source_images=[_image(tmp_path / "section_001_01.png")],
        )
    )
    assert out is not None
    priced = [e for e in costs.get_tracker().events
              if e.model == "fal-ai/gemini-25-flash-image/edit"]
    assert len(priced) == 1
    assert priced[0].estimated_usd == pytest.approx(0.039)


# ── one provider, and only for thumbnails ────────────────────────────

def test_no_other_image_provider_is_reachable_from_the_thumbnail_path():
    """FLUX and the rest must not appear on this path."""
    from pathlib import Path as _P

    text = (
        _P("core/providers/thumbnails.py").read_text(encoding="utf-8")
        + _P("core/thumbnailer.py").read_text(encoding="utf-8")
    ).lower()
    for banned in ("flux", "schnell", "stability", "midjourney", "dall", "replicate"):
        assert banned not in text, f"{banned} must not be on the thumbnail path"


def test_scene_sourcing_never_reaches_the_thumbnail_editor():
    """Thumbnails stay separate from normal visual generation."""
    from pathlib import Path as _P

    sourcer = _P("core/image_sourcer.py").read_text(encoding="utf-8")
    assert "edit_thumbnail_image" not in sourcer
    assert "providers.thumbnails" not in sourcer
