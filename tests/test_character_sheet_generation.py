"""The sheet has to be drawn the way the scenes are drawn.

A reference test made this concrete. Scenes were generated on the channel's
primary model (gemini-2.5-flash-image) with the channel's illustration style
appended; the reference sheets were generated on its *fallback* model
(fal-ai/flux/schnell) with only the module's own style block. The two models do
not share a house style, so the sheet came back as a polished animated-feature
character while the scenes were stick figures -- and the consistency reviewer
rejected eight scenes for not matching a sheet that was itself off-style.

The other half is the reviewer's own answer. It is asked for an object and
sometimes replies with the bare array of rejected filenames. That is a readable
verdict, and it used to raise `'list' object has no attribute 'get'` and take
the whole image_source stage down with it.

Neither fix may loosen the gate: a rejection still rejects, and a name that
does not belong to this run's scenes is still discarded.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import clients
from core import animation_stage
from core import character_bible as cb
from core.utils import Script, ScriptSection, VisualSlot


def _config():
    from core.utils import load_channel_config

    return load_channel_config("animated_stories")


def _script():
    return Script(
        title="A story",
        video_type="story",
        sections=[
            ScriptSection(
                id=1,
                narration="Sam waited by the door.",
                estimated_duration_seconds=10.0,
                slots=[VisualSlot(visual="ai_illustration", prompt="a scene", keywords="scene")],
            )
        ],
    )


# --- the sheet is drawn on the scene generator ------------------------------


def _capture_sheet_calls(monkeypatch, tmp_path):
    """Run the character stage with the generator and the model stubbed out."""
    calls: list[dict] = []

    async def fake_generate(prompt, output_path, **kwargs):
        calls.append({"prompt": prompt, "model": kwargs.get("model")})
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"sheet")
        return output_path

    async def fake_bible(script, plan, *, generate_text, limit=4):
        return cb.bible_from_entries(
            [{"name": "Sam", "role": "main", "face": "round white head",
              "hair": "black", "clothing": "hoodie", "colours": "navy",
              "accessories": "none", "proportions": "thin", "expression": "wary"}]
        )

    monkeypatch.setattr(clients, "generate_scene_image", fake_generate)
    monkeypatch.setattr(animation_stage.bible_mod, "build_bible", fake_bible)

    asyncio.run(
        animation_stage.build_character_sheet(
            _script(), _config(), tmp_path, plan={"topic": "t"}
        )
    )
    return calls


def test_the_sheet_uses_the_same_model_as_the_scenes(monkeypatch, tmp_path):
    config = _config()
    calls = _capture_sheet_calls(monkeypatch, tmp_path)

    assert calls, "no sheet was drawn"
    assert calls[0]["model"] == config.image_sourcing.generation_model
    assert calls[0]["model"] != config.image_sourcing.generated_fallback_model, (
        "the sheet was drawn on the fallback model again"
    )


def test_the_sheet_carries_the_channels_own_style(monkeypatch, tmp_path):
    config = _config()
    calls = _capture_sheet_calls(monkeypatch, tmp_path)

    suffix = config.image_sourcing.illustration_style_prompt_suffix
    assert suffix, "this test needs the channel to declare an illustration style"
    assert suffix in calls[0]["prompt"], (
        "the sheet is drawn under a different style contract than the scenes"
    )


def test_the_locked_description_still_governs_the_sheet(monkeypatch, tmp_path):
    """Changing the renderer must not change what is being rendered."""
    calls = _capture_sheet_calls(monkeypatch, tmp_path)
    prompt = calls[0]["prompt"]

    assert "[char_01] Sam" in prompt
    assert "CLOTHING: hoodie" in prompt
    assert cb.IDENTITY_LOCK in prompt


def test_the_bible_is_still_written(monkeypatch, tmp_path):
    _capture_sheet_calls(monkeypatch, tmp_path)
    record = json.loads(
        (tmp_path / "character" / "character_bible.json").read_text(encoding="utf-8")
    )
    assert record["characters"][0]["sheet"] == "char_01_sheet.png"


def test_a_channel_with_no_primary_model_falls_back(monkeypatch, tmp_path):
    """An empty generation_model must not produce an empty model argument."""
    config = _config()
    config.image_sourcing.generation_model = ""

    calls: list[dict] = []

    async def fake_generate(prompt, output_path, **kwargs):
        calls.append({"model": kwargs.get("model")})
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"sheet")
        return output_path

    async def fake_bible(script, plan, *, generate_text, limit=4):
        return cb.bible_from_entries([{"name": "Sam", "role": "main"}])

    monkeypatch.setattr(clients, "generate_scene_image", fake_generate)
    monkeypatch.setattr(animation_stage.bible_mod, "build_bible", fake_bible)

    asyncio.run(
        animation_stage.build_character_sheet(_script(), config, tmp_path, plan=None)
    )
    assert calls[0]["model"] == config.image_sourcing.generated_fallback_model


# --- a bare array is a verdict, not a crash ---------------------------------


def _review_workspace(tmp_path, scenes=("section_001_01.jpg",)):
    char_dir = tmp_path / "character"
    char_dir.mkdir(parents=True, exist_ok=True)
    (char_dir / "char_01_sheet.png").write_bytes(b"sheet")
    (char_dir / "character_bible.json").write_text(
        json.dumps({"characters": [{
            "id": "char_01", "name": "Sam", "role": "main",
            "face": "f", "hair": "h", "clothing": "c", "colours": "col",
            "accessories": "none", "proportions": "p", "expression": "e",
            "sheet": "char_01_sheet.png",
        }]}),
        encoding="utf-8",
    )
    raw = tmp_path / "images" / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    paths = []
    for name in scenes:
        path = raw / name
        path.write_bytes(b"scene")
        paths.append(path)
    return paths


def _review(monkeypatch, tmp_path, response, scenes=("section_001_01.jpg",)):
    paths = _review_workspace(tmp_path, scenes)
    seen: dict = {}

    async def fake_vision(prompt, image_paths, **kwargs):
        """Stands in for the network only. The normalisation under test is the
        real one, so a stub cannot accidentally prove itself correct."""
        seen.update(kwargs)
        list_key = kwargs.get("list_key")
        if list_key is None:
            return response
        return clients.normalise_review_response(response, list_key)

    monkeypatch.setattr(clients, "review_with_vision", fake_vision)
    verdict = asyncio.run(
        animation_stage.review_character_consistency(
            _script(), _config(), tmp_path, image_paths=paths
        )
    )
    return verdict, seen


def test_the_review_asks_for_the_normalised_shape(monkeypatch, tmp_path):
    _, seen = _review(monkeypatch, tmp_path, {"rejected": [], "reason": "ok"})
    assert seen.get("list_key") == "rejected"


def test_a_bare_array_is_read_as_the_rejection_list(monkeypatch, tmp_path):
    """The exact response that crashed a real run."""
    verdict, _ = _review(monkeypatch, tmp_path, ["section_001_01.jpg"])
    assert verdict["passed"] is False, "a rejection was dropped"
    assert verdict["rejected"] == ["section_001_01.jpg"]


def test_an_empty_array_passes(monkeypatch, tmp_path):
    verdict, _ = _review(monkeypatch, tmp_path, [])
    assert verdict["passed"] is True
    assert verdict["rejected"] == []


def test_an_unreadable_verdict_defers_to_image_review(monkeypatch, tmp_path):
    """Same stance the module already takes for a reviewer that cannot run."""
    verdict, _ = _review(monkeypatch, tmp_path, "no")
    assert verdict["passed"] is True
    assert verdict["reason"] == "unreadable verdict"


def test_a_non_list_rejected_field_does_not_crash(monkeypatch, tmp_path):
    verdict, _ = _review(monkeypatch, tmp_path, {"rejected": "everything"})
    assert verdict["passed"] is True
    assert verdict["rejected"] == []


def test_the_object_form_still_works(monkeypatch, tmp_path):
    verdict, _ = _review(
        monkeypatch, tmp_path,
        {"rejected": ["section_001_01.jpg"], "reason": "different face"},
    )
    assert verdict["passed"] is False
    assert verdict["reason"] == "different face"


def test_a_name_this_run_does_not_own_is_still_discarded(monkeypatch, tmp_path):
    """The filter that keeps a hallucinated filename out must survive."""
    verdict, _ = _review(monkeypatch, tmp_path, ["section_099_09.jpg"])
    assert verdict["rejected"] == []
    assert verdict["passed"] is True


def test_only_the_named_scene_of_several_is_rejected(monkeypatch, tmp_path):
    verdict, _ = _review(
        monkeypatch, tmp_path, ["section_001_02.jpg"],
        scenes=("section_001_01.jpg", "section_001_02.jpg"),
    )
    assert verdict["rejected"] == ["section_001_02.jpg"]
    assert verdict["passed"] is False


# --- the normalisation itself, and its opt-in nature ------------------------


def test_normalisation_is_opt_in():
    """The other two callers pass no list_key and must keep their shape."""
    import inspect

    source = inspect.getsource(clients.review_with_vision)
    assert "if list_key is None:\n        return parsed_json" in source

    from pathlib import Path as P

    root = P(__file__).resolve().parent.parent
    reviewer = (root / "core" / "reviewer.py").read_text(encoding="utf-8")
    assert "isinstance(answer, list)" in reviewer, (
        "core.reviewer still normalises a list itself, so it must not be "
        "handed a pre-wrapped dict"
    )
    assert "list_key" not in reviewer
    sourcer = (root / "core" / "image_sourcer.py").read_text(encoding="utf-8")
    assert "list_key" not in sourcer


@pytest.mark.parametrize(
    "parsed,expected",
    [
        ({"rejected": ["a"]}, {"rejected": ["a"]}),
        (["a", "b"], {"rejected": ["a", "b"]}),
        ([], {"rejected": []}),
        ("nonsense", {}),
        (None, {}),
        (7, {}),
    ],
)
def test_the_normalisation_table(parsed, expected):
    assert clients.normalise_review_response(parsed, "rejected") == expected


def test_review_with_vision_uses_that_helper():
    import inspect

    source = inspect.getsource(clients.review_with_vision)
    assert "return normalise_review_response(" in source
