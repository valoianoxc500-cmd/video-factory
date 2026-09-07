"""image_source may not report success with a required asset missing.

Regression cover for a Horror run that recorded `image_source` complete after
2097.8s and then failed validation with "Missing raw image(s): section_002_01".

Three defects lined up to produce it:

  1. `clients.generate_image_gemini` returns None on a refusal, a safety block
     or a 429 rather than raising. The generated-fallback pass only caught
     exceptions, so three beats rate-limited in the same second were recorded
     as generated and marked sourced with no file written.
  2. That pass matched descriptors on `sub_index`, a key descriptors do not
     carry, so it read 0 for every beat -- marking the section's first
     unsourced descriptor regardless of which beat was generated, and marking
     nothing at all when the generated beat was not slot 1.
  3. The pipeline marked the stage complete and validated afterwards, so the
     checkpoint said `image_source` was done while `last_error` was cleared.
     Every resume then skipped sourcing and failed at the same line.
"""

import asyncio
import io

import pytest
from PIL import Image

import core.image_sourcer as image_sourcer
from core.utils import ChannelConfig, Script, ScriptSection, VisualSlot


def _jpg(path, size=(1080, 1920)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, (30, 40, 50)).save(path, format="JPEG")


def _config(**sourcing) -> ChannelConfig:
    config = ChannelConfig(
        channel_name="Test",
        niche={
            "category": "test",
            "focus": "test",
            "audience": "general",
            "content_style": "informative",
        },
        youtube={
            "tags": ["test"],
            "title_formats": [{"name": "question", "instruction": "question"}],
            "description_styles": [{"name": "short", "instruction": "short"}],
        },
        image_sourcing={
            "generation_model": "gemini-test",
            "style_prompt_suffix": "photo suffix",
        },
        thumbnail_strategies=[{"name": "hero", "instruction": "hero"}],
    )
    # Portrait, like the real channels: the beats here are 1080x1920, and the
    # minimum-source check is judged against this.
    config.video.resolution = [1080, 1920]
    for key, value in sourcing.items():
        setattr(config.image_sourcing, key, value)
    return config


def _script(slot_count: int = 2) -> Script:
    return Script(
        title="A story",
        video_type="story",
        sections=[
            ScriptSection(
                id=2,
                narration="narration",
                estimated_duration_seconds=10.0,
                slots=[
                    VisualSlot(
                        visual="ai_photo",
                        prompt=f"a scene {i}",
                        keywords=f"scene {i}",
                    )
                    for i in range(1, slot_count + 1)
                ],
            )
        ],
    )


def _descriptor(script: Script, sub_idx: int, raw_dir):
    section = script.sections[0]
    return {
        "section": section,
        "sub_idx": sub_idx,
        "slot": section.slots[sub_idx],
        "keywords": section.slots[sub_idx].keywords,
        "prompt": section.slots[sub_idx].prompt,
        "img_path": raw_dir / f"section_002_{sub_idx + 1:02d}.jpg",
        "b_roll": False,
        "lane": "photo",
        "sourced": False,
    }


# --- the generated fallback must not claim a file it did not write --------


def test_a_rate_limited_generation_is_not_a_sourced_beat(monkeypatch, tmp_path):
    """A None return means no file, whatever the budget counted."""
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    script = _script(1)
    descriptor = _descriptor(script, 0, raw_dir)

    async def rate_limited(prompt, output_path, **kwargs):
        # Exactly what the real client does on 429: logs, returns None, and
        # writes nothing.
        return None

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", rate_limited)

    log: list[dict] = []
    asyncio.run(
        image_sourcer._generate_missing_visuals(
            descriptors=[descriptor],
            config=_config(allow_generated_fallback=True),
            sourcing_log=log,
        )
    )

    assert descriptor["sourced"] is False
    assert not (raw_dir / "section_002_01.jpg").exists()
    assert log == [], "nothing was generated, so nothing may be logged as generated"


def test_a_generation_that_writes_a_file_does_mark_the_beat(monkeypatch, tmp_path):
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    script = _script(1)
    descriptor = _descriptor(script, 0, raw_dir)

    async def writes_a_file(prompt, output_path, **kwargs):
        _jpg(output_path)
        return output_path

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", writes_a_file)

    asyncio.run(
        image_sourcer._generate_missing_visuals(
            descriptors=[descriptor],
            config=_config(allow_generated_fallback=True),
            sourcing_log=[],
        )
    )

    assert descriptor["sourced"] is True
    assert descriptor["generated"] is True


def test_a_generated_beat_marks_its_own_slot_not_the_first_one(monkeypatch, tmp_path):
    """The `sub_index` key bug marked slot 1 for every beat in the section."""
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    script = _script(2)
    first = _descriptor(script, 0, raw_dir)
    second = _descriptor(script, 1, raw_dir)

    # Only the second beat's generation succeeds.
    async def only_the_second(prompt, output_path, **kwargs):
        if output_path.name == "section_002_02.jpg":
            _jpg(output_path)
            return output_path
        return None

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", only_the_second)

    asyncio.run(
        image_sourcer._generate_missing_visuals(
            descriptors=[first, second],
            config=_config(allow_generated_fallback=True),
            sourcing_log=[],
        )
    )

    assert first["sourced"] is False, "slot 1 has no file and must stay unsourced"
    assert second["sourced"] is True, "slot 2 has a file and must be marked"


# --- the stage reconciles against the filesystem before completing --------


def _reconcile(script, config, descriptors, tmp_path):
    return asyncio.run(
        image_sourcer._reconcile_expected_assets(
            script=script,
            config=config,
            descriptors=descriptors,
            sourcing_log=[],
            seen_hashes=set(),
            raw_dir=tmp_path / "images" / "raw",
            videos_dir=tmp_path / "videos" / "raw",
            target_size=(1080, 1920),
            fps=30,
        )
    )


def test_reconciliation_is_a_no_op_when_every_beat_has_a_file(tmp_path):
    raw_dir = tmp_path / "images" / "raw"
    script = _script(2)
    _jpg(raw_dir / "section_002_01.jpg")
    _jpg(raw_dir / "section_002_02.jpg")

    descriptors = [_descriptor(script, i, raw_dir) for i in range(2)]
    for desc in descriptors:
        desc["sourced"] = True

    assert _reconcile(script, _config(), descriptors, tmp_path) is False
    assert len(script.sections[0].slots) == 2


def test_a_beat_claimed_sourced_with_no_file_is_recovered(monkeypatch, tmp_path):
    """The exact section_002_01 shape: claimed sourced, nothing on disk."""
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    script = _script(2)
    _jpg(raw_dir / "section_002_02.jpg")

    descriptors = [_descriptor(script, i, raw_dir) for i in range(2)]
    for desc in descriptors:
        desc["sourced"] = True  # both claim success; only one has a file

    async def recovering_retry(*, descriptors, **kwargs):
        for desc in descriptors:
            _jpg(desc["img_path"])
            desc["sourced"] = True

    monkeypatch.setattr(image_sourcer, "_retry_missed_slots", recovering_retry)

    assert _reconcile(script, _config(), descriptors, tmp_path) is True
    assert (raw_dir / "section_002_01.jpg").exists()
    assert len(script.sections[0].slots) == 2, "a recovered beat is kept"


def test_an_unrecoverable_beat_is_removed_rather_than_reported_sourced(
    monkeypatch, tmp_path
):
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    script = _script(2)
    _jpg(raw_dir / "section_002_02.jpg")

    descriptors = [_descriptor(script, i, raw_dir) for i in range(2)]
    for desc in descriptors:
        desc["sourced"] = True

    async def nothing_recovers(*, descriptors, **kwargs):
        return None

    monkeypatch.setattr(image_sourcer, "_retry_missed_slots", nothing_recovers)
    monkeypatch.setattr(
        image_sourcer,
        "enforce_minimum_slots",
        lambda *a, **k: None,  # slot minimums are covered by their own tests
    )

    assert _reconcile(script, _config(), descriptors, tmp_path) is True

    # The beat is gone, and the survivor was renumbered into its place, so
    # every remaining beat is backed by a real file.
    assert len(script.sections[0].slots) == 1
    assert (raw_dir / "section_002_01.jpg").exists()
    assert image_sourcer._missing_expected_slots(
        script, _config(), raw_dir, tmp_path / "videos" / "raw"
    ) == []


def test_reconciliation_raises_rather_than_reporting_a_missing_asset(
    monkeypatch, tmp_path
):
    """If a beat can be neither filled nor removed, the stage must fail."""
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    script = _script(1)  # a lone slot cannot be dropped

    descriptor = _descriptor(script, 0, raw_dir)
    descriptor["sourced"] = True  # claims success with no file

    async def nothing_recovers(*, descriptors, **kwargs):
        return None

    monkeypatch.setattr(image_sourcer, "_retry_missed_slots", nothing_recovers)

    with pytest.raises(RuntimeError):
        _reconcile(script, _config(), [descriptor], tmp_path)


# --- a zero-byte file is not an asset ------------------------------------


def test_an_empty_file_does_not_count_as_a_sourced_beat(tmp_path):
    empty = tmp_path / "section_002_01.jpg"
    empty.write_bytes(b"")

    assert image_sourcer._is_usable_asset(empty) is False
    assert image_sourcer._is_usable_asset(tmp_path / "absent.jpg") is False
    assert image_sourcer._is_usable_asset(None) is False


def test_a_clip_counts_as_the_beats_asset(tmp_path):
    """Same rule the validator applies: an mp4 satisfies the beat."""
    videos_dir = tmp_path / "videos" / "raw"
    videos_dir.mkdir(parents=True)
    (videos_dir / "section_002_01.mp4").write_bytes(b"clip")

    assert image_sourcer._expected_asset_path(
        section_id=2,
        sub_idx=1,
        raw_dir=tmp_path / "images" / "raw",
        videos_dir=videos_dir,
    ) == (videos_dir / "section_002_01.mp4")


# --- the retry pass runs concurrently and under a clock -------------------


def test_slots_recover_concurrently(monkeypatch, tmp_path):
    """Sequential recovery is what made a 35-minute stage."""
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    script = _script(4)
    descriptors = [_descriptor(script, i, raw_dir) for i in range(4)]

    in_flight = 0
    peak = 0

    async def slow_recovery(*, desc, **kwargs):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.05)
        in_flight -= 1
        desc["sourced"] = True

    monkeypatch.setattr(image_sourcer, "_recover_missed_slot", slow_recovery)

    asyncio.run(
        image_sourcer._retry_missed_slots(
            descriptors=descriptors,
            script=script,
            config=_config(),
            client=object(),
            seen_hashes=set(),
            sourcing_log=[],
            raw_dir=raw_dir,
        )
    )

    assert peak > 1, "slots must not recover one at a time"
    assert all(desc["sourced"] for desc in descriptors)


def test_one_slow_slot_cannot_hold_the_pass_open(monkeypatch, tmp_path):
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    script = _script(2)
    descriptors = [_descriptor(script, i, raw_dir) for i in range(2)]

    monkeypatch.setattr(image_sourcer, "_SLOT_RETRY_TIMEOUT_SECONDS", 0.05)

    async def one_hangs(*, desc, **kwargs):
        if desc["sub_idx"] == 0:
            await asyncio.sleep(30)
        desc["sourced"] = True

    monkeypatch.setattr(image_sourcer, "_recover_missed_slot", one_hangs)

    asyncio.run(
        image_sourcer._retry_missed_slots(
            descriptors=descriptors,
            script=script,
            config=_config(),
            client=object(),
            seen_hashes=set(),
            sourcing_log=[],
            raw_dir=raw_dir,
        )
    )

    # The hung beat is given up on and left for the fallback passes; the other
    # one is not held hostage by it.
    assert descriptors[0]["sourced"] is False
    assert descriptors[1]["sourced"] is True


def test_a_raising_slot_does_not_take_the_pass_down(monkeypatch, tmp_path):
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True)
    script = _script(2)
    descriptors = [_descriptor(script, i, raw_dir) for i in range(2)]

    async def one_raises(*, desc, **kwargs):
        if desc["sub_idx"] == 0:
            raise RuntimeError("search exploded")
        desc["sourced"] = True

    monkeypatch.setattr(image_sourcer, "_recover_missed_slot", one_raises)

    asyncio.run(
        image_sourcer._retry_missed_slots(
            descriptors=descriptors,
            script=script,
            config=_config(),
            client=object(),
            seen_hashes=set(),
            sourcing_log=[],
            raw_dir=raw_dir,
        )
    )

    assert descriptors[0]["sourced"] is False
    assert descriptors[1]["sourced"] is True
