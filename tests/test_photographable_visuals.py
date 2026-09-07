"""Visual keywords must name something a real photograph could contain.

A web-photo-only channel cannot be handed a graphic device or an unphotographed
scene. The D.B. Cooper run failed at the image review gate for exactly this:

    "Several images failed due to incorrect subjects or styles (e.g. book
     covers or cartoons instead of cinematic photos) ... several specific
     requested details (like the tie on the seat or the red question mark)
     were missing."

The gate was right. These cover the upstream rule that stops such a slot being
written in the first place.
"""

import json
from pathlib import Path

import pytest

from core.scripter import non_photographable_reason
from core.utils import load_channel_config

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- conceptual requests are rejected --------------------------------------

@pytest.mark.parametrize(
    "keywords",
    [
        "red question mark",
        "large question mark on dark background",
        "unsolved mystery background",
        "mysterious background texture",
        "spooky atmosphere backdrop",
        "D.B. Cooper book cover",
        "true crime podcast thumbnail",
        "hijacking movie poster",
        "cartoon of a hijacker",
        "concept art of the jump",
        "artist's impression of the parachute descent",
        "digital art of a plane at night",
        "staged recreation of the ransom handover",
        "re-enactment of the hijacking",
        "silhouette of a mysterious man",
        "shadowy figure in an airport",
        "red circle highlighting the seat",
        "cinematic shot of a runway",
    ],
)
def test_conceptual_requests_are_rejected(keywords):
    assert non_photographable_reason(keywords), f"{keywords!r} should be rejected"


def test_the_two_slots_that_actually_failed_are_rejected():
    """The review gate named these; the planner must never ask for them again."""
    assert non_photographable_reason("red question mark")
    assert non_photographable_reason("unsolved mystery background")


# --- real documentary subjects are preserved -------------------------------

@pytest.mark.parametrize(
    "keywords",
    [
        "Boeing 727 Northwest Orient Airlines 1971",
        "Portland International Airport 1971",
        "Seattle Tacoma airport terminal 1971",
        "Columbia River Tina Bar sandbar",
        "twenty dollar bills bundle 1971",
        "parachute rigging equipment",
        "FBI agents searching woodland Washington state",
        "aft airstair Boeing 727 deployed",
        "Lewis River Washington forest aerial",
        "newspaper front page 1971 hijacking",
    ],
)
def test_real_documentary_subjects_are_allowed(keywords):
    assert non_photographable_reason(keywords) == "", f"{keywords!r} should pass"


# --- archival artefacts survive, including drawings ------------------------

@pytest.mark.parametrize(
    "keywords",
    [
        "FBI composite sketch of D.B. Cooper",
        "police sketch of the suspect",
        "wanted poster D.B. Cooper",
        "FBI evidence photograph of the clip-on tie",
        "FBI case file document D.B. Cooper",
        "court document hijacking case",
        "archival photograph of the aircraft",
        "newspaper clipping 1971 skyjacking",
    ],
)
def test_archival_artefacts_are_preserved(keywords):
    """A composite sketch is a real case record even though it is a drawing."""
    assert non_photographable_reason(keywords) == "", f"{keywords!r} must stay allowed"


def test_a_graphic_device_is_not_excused_by_naming_a_real_artefact():
    """The allowlist must exempt artwork wording, not neutralise a device.

    The slot that reached search was "question mark D.B. Cooper composite
    sketch": the allowlist matched "composite sketch" and returned early, so
    image search was sent looking for a photograph containing a question mark.
    """
    reason = non_photographable_reason("question mark D.B. Cooper composite sketch")
    assert reason, "a question mark must be rejected even beside a real artefact"
    assert "question mark" in reason


@pytest.mark.parametrize(
    "keywords",
    [
        "question mark over FBI case file",
        "wanted poster with a red circle",
        "newspaper clipping mystery background",
        "police sketch cinematic lighting",
        "archival photograph staged recreation",
    ],
)
def test_devices_are_rejected_even_alongside_archival_terms(keywords):
    assert non_photographable_reason(keywords), f"{keywords!r} should be rejected"


def test_sketch_is_allowed_only_when_named_as_the_composite():
    """The artefact is allowed; a generic drawing of the man is not."""
    assert non_photographable_reason("FBI composite sketch of the suspect") == ""
    assert non_photographable_reason("artist's impression of D.B. Cooper's face")


def test_evidence_must_be_requested_as_the_archival_photograph():
    """A staged close-up of the tie is a fabricated document; the real one is not."""
    assert non_photographable_reason("staged close-up of the tie on the seat")
    assert non_photographable_reason("FBI evidence photograph of the clip-on tie") == ""


# --- the rule is inert where it should be ----------------------------------

def test_empty_keywords_are_not_flagged():
    assert non_photographable_reason("") == ""
    assert non_photographable_reason(None) == ""


def test_football_style_keywords_are_unaffected():
    """The rule must not start rejecting the other engine's ordinary slots."""
    for keywords in (
        "Anfield stadium crowd",
        "Alexander Isak celebrating",
        "Premier League trophy presentation",
        "Portman Road stadium exterior",
    ):
        assert non_photographable_reason(keywords) == "", keywords


# --- channel guidance -------------------------------------------------------

def _horror_visual_guidance() -> dict:
    raw = json.loads(
        (REPO_ROOT / "config" / "channels" / "horror_stories.json")
        .read_text(encoding="utf-8")
    )
    return raw["video"]["visual_guidance"]


def test_horror_declares_the_photographable_subject_rule():
    guidance = _horror_visual_guidance()
    assert "photographable_subjects_only" in guidance
    assert "archival" in guidance["photographable_subjects_only"].lower()


def test_horror_forbids_the_specific_devices_that_failed():
    forbidden = _horror_visual_guidance()["forbidden_visuals"].lower()
    for term in ("question mark", "book cover", "podcast", "cartoon",
                 "re-enactment", "silhouette", "cinematic"):
        assert term in forbidden, f"{term!r} is not forbidden"


def test_horror_keeps_the_composite_sketch_allowed_but_labelled():
    archival = _horror_visual_guidance()["archival_evidence"].lower()
    assert "composite sketch" in archival
    assert "never present it" in archival or "never" in archival


def test_horror_handles_cases_with_no_photograph_of_the_person():
    assert "thin_photographic_record" in _horror_visual_guidance()


# --- nothing was relaxed to achieve this -----------------------------------

def test_web_photos_only_is_still_on():
    assert load_channel_config("horror_stories").image_sourcing.web_photos_only is True


def test_no_illustration_fallback_was_opened():
    """Tightening the planner must not have quietly opened a generated lane."""
    guidance = _horror_visual_guidance()
    # A generated photo of a real case is a fabricated document, always.
    assert guidance["ai_photo"].startswith("Never")
    # Illustration stays confined to openly fictional content -- it is not a
    # fallback for a true case whose photographic record is thin.
    illustration = guidance["ai_illustration"].lower()
    assert "fallback only" in illustration
    assert "fictional" in illustration
    assert "never as a photograph" in illustration


def test_review_gate_and_hold_cap_are_untouched():
    cfg = load_channel_config("horror_stories")
    assert cfg.review_thresholds.image_review_max_attempts == 2
    assert cfg.rendering_defaults.max_visual_hold_seconds == 5.0


# --- the rule is wired into script validation ------------------------------

def _script_with_keywords(keywords: str) -> dict:
    return {
        "sections": [
            {
                "id": 1,
                "narration": "w " * 30,
                "slots": [
                    {"visual": "google_photo", "keywords": "Boeing 727 1971", "prompt": "p"},
                    {"visual": "google_photo", "keywords": keywords, "prompt": "p"},
                ],
            }
        ]
    }


def _errors_for(keywords: str, *, web_photos_only: bool) -> list[str]:
    from core.scripter import _script_validation_errors
    from core.utils import script_timing_profile

    errors, _ = _script_validation_errors(
        _script_with_keywords(keywords),
        numbering_order=None,
        max_visual_hold_seconds=5.0,
        crossfade=0.3,
        timing_profile=script_timing_profile("gemini-2.5-flash-tts"),
        web_photos_only=web_photos_only,
    )
    return errors


def test_validation_rejects_a_conceptual_slot_on_a_web_photo_channel():
    errors = _errors_for("red question mark", web_photos_only=True)
    assert any("cannot be sourced as a real photograph" in e for e in errors)


def test_validation_tells_the_scripter_what_to_write_instead():
    """The error has to be actionable -- it drives the revision retry."""
    errors = _errors_for("unsolved mystery background", web_photos_only=True)
    guidance = " ".join(errors).lower()
    assert "concrete real-world subject" in guidance
    assert "archival" in guidance


def _sourcing_errors(keywords: str, *, web_photos_only: bool) -> list[str]:
    """Only this rule's errors. The fixture also trips the pacing rule, which
    is a separate, correct complaint about slot count."""
    return [
        e for e in _errors_for(keywords, web_photos_only=web_photos_only)
        if "cannot be sourced" in e
    ]


def test_validation_accepts_a_real_subject():
    assert _sourcing_errors("Columbia River sandbar", web_photos_only=True) == []


def test_validation_accepts_the_composite_sketch():
    assert _sourcing_errors(
        "FBI composite sketch of the suspect", web_photos_only=True
    ) == []


def test_rule_is_inert_when_the_channel_allows_generated_images():
    """Only a web-photo-only channel is constrained to what a camera captured."""
    assert _sourcing_errors("red question mark", web_photos_only=False) == []


def test_football_news_guidance_was_not_touched():
    football = json.loads(
        (REPO_ROOT / "config" / "channels" / "football_news.json")
        .read_text(encoding="utf-8")
    )
    assert "photographable_subjects_only" not in football["video"]["visual_guidance"]
