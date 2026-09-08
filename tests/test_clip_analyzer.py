"""Clip Analyzer: explains a video, and does not rebuild it.

Two things are worth guarding. First, honesty about depth: the analyser must
not describe shots when it was never shown any. Second, that it stays an
analysis -- it writes nothing back onto the asset and queues no generation,
which is the whole difference between this and the Re-Create flow it replaced.
"""

from __future__ import annotations

import asyncio

import pytest

from viral import explain as ex
from viral.analysis import AnalysisDepth
from viral.rights import Source, attest

VIDEO = {
    "platform": "youtube",
    "title": "The bridge nobody crosses",
    "author": "channelname",
    "duration_seconds": 41.0,
    "views": 1_200_000,
    "likes": 88_000,
    "comments": 4_100,
    "followers": 250_000,
}

FULL_PAYLOAD = {
    "headline": "It opens mid-sentence, so there is nothing to skip.",
    "sections": {key: f"text for {key}" for key, _ in ex.SECTIONS},
    "plan": ["Step one", "Step two", "Step three"],
    "limitations": "",
}


# ── the prompt tells the model what it may claim ─────────────────────

def test_prompt_without_frames_forbids_describing_shots():
    prompt = ex.build_prompt(VIDEO, [], saw_frames=False)
    assert "have NOT seen the video" in prompt
    assert "Do not describe shots" in prompt


def test_prompt_with_frames_cites_the_sample_times():
    prompt = ex.build_prompt(VIDEO, [0.3, 1.5, 20.0], saw_frames=True)
    assert "0.3s, 1.5s, 20.0s" in prompt
    assert "You have frames" in prompt


def test_prompt_asks_for_every_section_the_ui_renders():
    prompt = ex.build_prompt(VIDEO, [], saw_frames=False)
    for key, _ in ex.SECTIONS:
        assert f'"{key}"' in prompt


def test_prompt_refuses_to_write_the_script_for_them():
    prompt = ex.build_prompt(VIDEO, [], saw_frames=False)
    assert "Do not write the script" in prompt
    assert "do not reproduce this video's wording" in prompt


def test_the_eight_sections_are_the_ones_that_were_asked_for():
    assert [key for key, _ in ex.SECTIONS] == [
        "why_it_performed", "hook", "structure", "pacing",
        "visuals", "captions", "audio", "improvements",
    ]


# ── depth ─────────────────────────────────────────────────────────────

def run(video, **kwargs):
    return asyncio.run(ex.explain(video, **kwargs))


def test_no_file_means_metadata_depth_even_when_rights_allow_more(tmp_path):
    seen = {}

    async def model_call(prompt, **_):
        seen["prompt"] = prompt
        return FULL_PAYLOAD

    result = run(
        VIDEO,
        attestation=attest(source=Source.OWNED_OR_PERMITTED, user_id="u1"),
        source_path=None,
        model_call=model_call,
    )
    assert result.depth == AnalysisDepth.METADATA.value
    assert result.frames_examined == 0
    assert "have NOT seen the video" in seen["prompt"]


def test_a_missing_file_does_not_claim_frames(tmp_path):
    async def model_call(prompt, **_):
        return FULL_PAYLOAD

    result = run(
        VIDEO,
        attestation=attest(source=Source.OWNED_OR_PERMITTED, user_id="u1"),
        source_path=tmp_path / "nope.mp4",
        model_call=model_call,
    )
    assert result.depth == AnalysisDepth.METADATA.value
    assert result.frames_examined == 0


def test_metadata_depth_always_states_what_it_could_not_see():
    async def model_call(prompt, **_):
        return {**FULL_PAYLOAD, "limitations": ""}

    result = run(VIDEO, model_call=model_call)
    assert "could not be observed" in result.limitations


def test_a_discovered_video_never_reaches_frame_depth(tmp_path):
    """DISCOVERED keeps analysis at metadata depth; the file is never read."""
    video_file = tmp_path / "clip.mp4"
    video_file.write_bytes(b"not really a video")

    async def model_call(prompt, **_):
        return FULL_PAYLOAD

    result = run(
        VIDEO,
        attestation=attest(source=Source.DISCOVERED, user_id="u1"),
        source_path=video_file,
        model_call=model_call,
    )
    assert result.depth == AnalysisDepth.METADATA.value


# ── parsing ───────────────────────────────────────────────────────────

def test_a_complete_answer_is_kept_whole():
    async def model_call(prompt, **_):
        return FULL_PAYLOAD

    result = run(VIDEO, model_call=model_call)
    assert result.headline.startswith("It opens mid-sentence")
    assert len(result.sections) == len(ex.SECTIONS)
    assert result.plan == ["Step one", "Step two", "Step three"]


def test_missing_sections_are_dropped_rather_than_rendered_empty():
    async def model_call(prompt, **_):
        return {"sections": {"hook": "good hook", "pacing": "   "}, "plan": []}

    result = run(VIDEO, model_call=model_call)
    assert set(result.sections) == {"hook"}
    assert result.plan == []


def test_a_junk_response_does_not_raise():
    async def model_call(prompt, **_):
        return {"sections": "not a dict", "plan": "not a list"}

    result = run(VIDEO, model_call=model_call)
    assert result.sections == {}
    assert result.plan == []


def test_an_empty_response_does_not_raise():
    async def model_call(prompt, **_):
        return None

    result = run(VIDEO, model_call=model_call)
    assert result.sections == {}


def test_the_record_is_json_shaped_for_the_task_result():
    async def model_call(prompt, **_):
        return FULL_PAYLOAD

    record = run(VIDEO, model_call=model_call).to_record()
    assert record["depth"] == "metadata"
    assert isinstance(record["sections"], dict)
    assert isinstance(record["plan"], list)


# ── it analyses, it does not recreate ────────────────────────────────

def test_the_analyzer_exposes_no_generation_entry_point():
    """Re-Create built a new video from an old one. This must not."""
    forbidden = {"recreate", "generate_video", "build_script", "queue_job"}
    assert forbidden.isdisjoint(dir(ex))


def test_run_explain_writes_nothing_back_onto_the_asset():
    """The result lives on the task, so re-running cannot alter an asset."""
    import viral.runner as runner

    calls: list[str] = []

    class FakeClient:
        def call(self, name, **kwargs):
            calls.append(name)
            if name == "asset":
                return {
                    "asset": {
                        "id": "a1",
                        "user_id": "u1",
                        "title": "A clip",
                        "source_platform": "vimeo",
                        "source_video_id": "",
                        "source_author": "someone",
                        "rights_source": "owned_or_permitted",
                        "rights_holder": "u1",
                        "duration_seconds": 30.0,
                        "storage_path": "",
                        "processed_path": "",
                    }
                }
            return {}

    async def fake_explain(video, **kwargs):
        return ex.Explanation(headline="ok", sections={"hook": "x"})

    saved = runner.explain
    runner.explain = fake_explain
    try:
        result = runner.run_explain(FakeClient(), {"asset_id": "a1"})
    finally:
        runner.explain = saved

    # Read the asset, and nothing else. No save_asset_analysis, no task queued.
    assert calls == ["asset"]
    assert result["asset_id"] == "a1"
    assert result["headline"] == "ok"


def test_an_unusable_rights_basis_degrades_instead_of_failing_the_task():
    """`attest` refuses an unknown source; that must cost depth, not the run."""
    import viral.runner as runner

    seen: dict = {}

    class FakeClient:
        def call(self, name, **kwargs):
            if name == "asset":
                return {
                    "asset": {
                        "id": "a1",
                        "user_id": "u1",
                        "title": "A clip",
                        "source_platform": "vimeo",
                        "source_video_id": "",
                        "source_author": "someone",
                        # Not a Source value at all.
                        "rights_source": "whatever",
                        "duration_seconds": 30.0,
                        "storage_path": __file__,
                        "processed_path": "",
                    }
                }
            return {}

    async def fake_explain(video, **kwargs):
        seen["source_path"] = kwargs.get("source_path")
        seen["depth"] = kwargs["attestation"].source
        return ex.Explanation(headline="ok")

    saved = runner.explain
    runner.explain = fake_explain
    try:
        result = runner.run_explain(FakeClient(), {"asset_id": "a1"})
    finally:
        runner.explain = saved

    assert result["headline"] == "ok"
    assert seen["depth"] is Source.DISCOVERED
    # No basis means the file is not read, even though one exists on disk.
    assert seen["source_path"] is None


def test_run_explain_refuses_an_asset_that_is_gone():
    import viral.runner as runner

    class FakeClient:
        def call(self, name, **kwargs):
            return {"asset": None}

    with pytest.raises(RuntimeError, match="no longer exists"):
        runner.run_explain(FakeClient(), {"asset_id": "missing"})
