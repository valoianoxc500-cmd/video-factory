"""Rights gate, processing limits, publishing capabilities and scheduling.

The rights tests are the load-bearing ones. This product processes and
republishes video, which is only legitimate when the operator holds rights to
it, so the gate has to be the thing that cannot be walked around.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from viral.processing import (
    PLATFORM_PROFILES,
    SourceProbe,
    build_ffmpeg_command,
    build_plan,
)
from viral.publishing import (
    CAPABILITIES,
    Capability,
    PublishRequest,
    PublishStatus,
    UnsupportedAdapter,
    backoff_seconds,
    capability_report,
    is_retryable,
    plan_publication,
    validate_request,
)
from viral.rights import (
    RightsError,
    Source,
    attest,
    is_prohibited_technique,
    require_publishable,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
USER = "11111111-1111-1111-1111-111111111111"


def _own(**over):
    params = dict(source=Source.OWN_RECORDING, user_id=USER, now=NOW)
    params.update(over)
    return attest(**params)


# --- rights ----------------------------------------------------------------

def test_own_footage_is_publishable():
    assert require_publishable(_own()).is_publishable is True


def test_a_discovered_video_can_never_be_published():
    """Discovery output is reference material, not a publishing input."""
    found = attest(source=Source.DISCOVERED, user_id=USER, now=NOW)
    assert found.is_publishable is False
    with pytest.raises(RightsError, match="reference material"):
        require_publishable(found)


def test_no_attestation_at_all_is_refused():
    with pytest.raises(RightsError, match="No rights attestation"):
        require_publishable(None)


def test_creative_commons_requires_naming_who_to_credit():
    bare = attest(source=Source.CREATIVE_COMMONS, user_id=USER, now=NOW)
    with pytest.raises(RightsError, match="crediting the rights holder"):
        require_publishable(bare)
    credited = attest(
        source=Source.CREATIVE_COMMONS, user_id=USER,
        rights_holder="A. Creator", now=NOW,
    )
    assert require_publishable(credited).needs_attribution is True


@pytest.mark.parametrize("source", [Source.LICENSED, Source.WRITTEN_PERMISSION])
def test_licensed_and_permissioned_footage_needs_evidence(source):
    holder = "Rights Co" if source is Source.WRITTEN_PERMISSION else ""
    with pytest.raises(RightsError, match="requires evidence"):
        require_publishable(attest(
            source=source, user_id=USER, rights_holder=holder, now=NOW,
        ))


def test_an_attestation_records_who_made_it():
    with pytest.raises(RightsError):
        attest(source=Source.OWN_RECORDING, user_id="", now=NOW)


def test_public_domain_needs_no_credit_or_evidence():
    assert require_publishable(
        attest(source=Source.PUBLIC_DOMAIN, user_id=USER, now=NOW)
    ).needs_attribution is False


# --- refusals that must not be quietly removed -----------------------------

@pytest.mark.parametrize("technique", [
    "content id evasion",
    "fingerprint perturbation",
    "duplicate detection evasion",
    "watermark removal",
    "metadata laundering",
    "mirror flip to avoid matching",
    "bypass copyright detection",
    "spoof the fingerprint",
    "circumvent duplicate detection",
])
def test_evasion_techniques_are_recognised(technique):
    assert is_prohibited_technique(technique) is True


@pytest.mark.parametrize("legitimate", [
    "re-encode", "normalise audio", "correct aspect ratio",
    "safe 9:16 framing", "minor crop", "trim silence", "optimise quality",
    "platform-specific encoding", "",
])
def test_legitimate_processing_is_not_blocked(legitimate):
    assert is_prohibited_technique(legitimate) is False


def test_a_plan_refuses_a_requested_evasion_step():
    with pytest.raises(RightsError, match="Refusing the requested step"):
        build_plan(
            SourceProbe(1920, 1080, 30.0, 30.0, True), "tiktok",
            attestation=_own(), requested_steps=["mirror flip to avoid matching"],
        )


# --- processing ------------------------------------------------------------

def test_processing_requires_rights_first():
    with pytest.raises(RightsError):
        build_plan(SourceProbe(1080, 1920, 20.0, 30.0, True), "tiktok")


def test_a_landscape_source_is_reframed_not_letterboxed():
    plan = build_plan(
        SourceProbe(1920, 1080, 30.0, 30.0, True), "instagram",
        attestation=_own(),
    )
    names = [s.name for s in plan.steps]
    assert "reframe" in names
    assert "re-encode" in names


def test_a_vertical_source_is_only_scaled():
    plan = build_plan(
        SourceProbe(1080, 1920, 30.0, 30.0, True), "tiktok", attestation=_own(),
    )
    names = [s.name for s in plan.steps]
    assert "scale" in names and "reframe" not in names


def test_audio_is_normalised_when_present_and_skipped_when_not():
    with_audio = build_plan(
        SourceProbe(1080, 1920, 20.0, 30.0, True), "tiktok", attestation=_own())
    silent = build_plan(
        SourceProbe(1080, 1920, 20.0, 30.0, False), "tiktok", attestation=_own())
    assert any(s.name == "normalise audio" for s in with_audio.steps)
    assert not any(s.name == "normalise audio" for s in silent.steps)
    assert any("no audio" in w.lower() for w in silent.warnings)


def test_over_length_video_warns_rather_than_silently_cutting():
    plan = build_plan(
        SourceProbe(1080, 1920, 300.0, 30.0, True), "instagram",
        attestation=_own(),
    )
    assert any("exceeds" in w for w in plan.warnings)
    assert any("will not silently cut" in w for w in plan.warnings)


def test_upscaling_is_warned_about():
    plan = build_plan(
        SourceProbe(480, 854, 20.0, 30.0, True), "tiktok", attestation=_own())
    assert any("cannot add detail" in w for w in plan.warnings)


def test_an_unknown_platform_is_rejected():
    with pytest.raises(ValueError, match="unknown platform"):
        build_plan(SourceProbe(1080, 1920, 20.0, 30.0, True), "myspace",
                   attestation=_own())


def test_the_command_targets_vertical_and_is_web_ready():
    plan = build_plan(
        SourceProbe(1920, 1080, 30.0, 30.0, True), "youtube", attestation=_own())
    cmd = build_ffmpeg_command(Path("in.mp4"), Path("out.mp4"), plan)
    joined = " ".join(cmd)
    assert "1080:1920" in joined
    assert "libx264" in joined and "yuv420p" in joined
    assert "+faststart" in joined
    assert "loudnorm" in joined


def test_the_command_crops_toward_the_subject():
    plan = build_plan(
        SourceProbe(1920, 1080, 30.0, 30.0, True), "tiktok", attestation=_own())
    left = " ".join(build_ffmpeg_command(Path("i"), Path("o"), plan, crop_centre_x=0.15))
    right = " ".join(build_ffmpeg_command(Path("i"), Path("o"), plan, crop_centre_x=0.85))
    assert left != right, "the crop ignored the subject position"


def test_every_platform_has_an_encoding_profile():
    for platform in ("instagram", "facebook", "tiktok", "youtube", "snapchat"):
        profile = PLATFORM_PROFILES[platform]
        assert profile.width == 1080 and profile.height == 1920


# --- publishing capabilities ----------------------------------------------

def test_snapchat_is_declared_unsupported_not_broken():
    """It has no server-side publishing API; a fake job would fail later."""
    cap = CAPABILITIES["snapchat"]
    assert Capability.UNSUPPORTED in cap.capabilities
    assert cap.can_publish is False
    result = UnsupportedAdapter("snapchat").publish(
        PublishRequest(USER, "v1", "path.mp4"), "token")
    assert result.status is PublishStatus.UNSUPPORTED
    assert result.retryable is False


def test_youtube_and_facebook_schedule_natively():
    for platform in ("youtube", "facebook"):
        assert Capability.NATIVE_SCHEDULING in CAPABILITIES[platform].capabilities


def test_instagram_and_tiktok_are_scheduled_by_our_queue():
    for platform in ("instagram", "tiktok"):
        cap = CAPABILITIES[platform]
        assert Capability.NATIVE_SCHEDULING not in cap.capabilities
        assert cap.can_schedule is True


def test_the_capability_report_tells_the_user_before_they_schedule():
    report = {r["platform"]: r for r in capability_report()}
    assert report["snapchat"]["supported"] is False
    assert report["snapchat"]["note"]
    assert report["youtube"]["native_scheduling"] is True
    assert report["tiktok"]["requires"]


# --- request validation and planning --------------------------------------

def _request(**over):
    params = dict(
        user_id=USER, video_id="v1", media_path="vrf/u/v1/processed.mp4",
        caption="A caption", platforms=("youtube", "instagram"),
    )
    params.update(over)
    return PublishRequest(**params)


def test_a_valid_request_has_no_problems():
    assert validate_request(_request(), now=NOW) == []


def test_selecting_snapchat_is_refused_up_front():
    problems = validate_request(_request(platforms=("snapchat",)), now=NOW)
    assert problems and "snapchat" in problems[0].lower()


def test_no_platform_selected_is_refused():
    assert validate_request(_request(platforms=()), now=NOW)


def test_a_past_schedule_is_refused():
    problems = validate_request(
        _request(scheduled_for=NOW - timedelta(hours=1)), now=NOW)
    assert any("past" in p for p in problems)


def test_a_naive_schedule_time_is_refused():
    problems = validate_request(
        _request(scheduled_for=datetime(2026, 12, 1, 9, 0)), now=NOW)
    assert any("timezone" in p for p in problems)


def test_scheduling_too_far_out_is_refused():
    problems = validate_request(
        _request(scheduled_for=NOW + timedelta(days=400)), now=NOW)
    assert any("180 days" in p for p in problems)


def test_planning_produces_one_job_per_platform():
    jobs = plan_publication(_request(), now=NOW)
    assert {j["platform"] for j in jobs} == {"youtube", "instagram"}
    assert all(j["status"] == PublishStatus.QUEUED.value for j in jobs)


def test_planning_uses_native_scheduling_where_it_exists():
    jobs = {
        j["platform"]: j for j in plan_publication(
            _request(platforms=("youtube", "instagram"),
                     scheduled_for=NOW + timedelta(days=1)), now=NOW)
    }
    assert jobs["youtube"]["mode"] == "native_schedule"
    assert jobs["instagram"]["mode"] == "queue_until_due"


def test_an_unsupported_platform_is_planned_as_unsupported():
    jobs = plan_publication(_request(platforms=("snapchat",)), now=NOW)
    assert jobs[0]["status"] == PublishStatus.UNSUPPORTED.value
    assert jobs[0]["reason"]


def test_attribution_is_added_to_the_caption():
    request = _request(caption="Great clip", attribution="Credit: A. Creator")
    assert "A. Creator" in request.caption_for("instagram")


def test_attribution_is_not_duplicated():
    request = _request(caption="Credit: A. Creator made this",
                       attribution="Credit: A. Creator")
    assert request.caption_for("instagram").count("A. Creator") == 1


def test_captions_are_trimmed_to_each_platform_limit():
    request = _request(caption="x" * 9000)
    assert len(request.caption_for("instagram")) <= 2200
    assert len(request.caption_for("youtube")) <= 5000


# --- retry -----------------------------------------------------------------

@pytest.mark.parametrize("code", [408, 429, 500, 502, 503, 504])
def test_transient_failures_retry(code):
    assert is_retryable(code) is True


@pytest.mark.parametrize("code", [400, 401, 403, 404, 422])
def test_permanent_failures_do_not_retry(code):
    """Repeating a rejected request burns the user's rate-limit budget."""
    assert is_retryable(code) is False


def test_rate_limit_messages_retry():
    assert is_retryable(None, "Rate limit exceeded, try again later") is True


def test_backoff_grows_and_is_capped():
    delays = [backoff_seconds(a) for a in range(1, 8)]
    assert delays == sorted(delays)
    assert delays[-1] <= 3600
    assert delays[0] >= 30
