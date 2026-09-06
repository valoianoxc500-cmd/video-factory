"""Charts and diagrams are caught before sourcing pays for them.

From a real Football News failure: three slots asked for a diagram, typed as
google_photo on a channel where every photo slot must be a real photograph.
Image search returned renders and infographics, the relevance gate correctly
rejected all of them, eleven beats were dropped, and the run died for want of
images it was never going to find. 35 of 55 rejections that run were "digital
graphics, logos, or illustrations rather than real photographs".

The guard that should have caught this caught none of it -- not the Arabic, and
not plain English "diagram" or "infographic" either.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from core.scripter import (  # noqa: E402
    conceptual_brief_reason,
    non_photographable_reason,
)

# The four briefs from the failed run, kept verbatim.
AR_CHART_ARROWS = "رسم بياني تأثير متسلسل أسهم هابطة"
AR_CHART_MONEY = "رسم بياني تدفق أموال تشيلسي بنفيكا"
AR_CHART_INFLATION = "رسم بياني سهم أحمر تضخم أسعار"
AR_DOMINO = "تأثير الدومينو شعارات أندية كرة قدم صورة فوتوغرافية"


# --- the exact briefs that failed in production ----------------------------

@pytest.mark.parametrize(
    "label,brief",
    [
        ("S1.6 cascading arrows", AR_CHART_ARROWS),
        ("S2.3 money flow", AR_CHART_MONEY),
        ("S2.5 price inflation", AR_CHART_INFLATION),
        ("S2.6 domino of badges", AR_DOMINO),
    ],
)
def test_the_briefs_that_broke_the_run_are_now_caught(label, brief):
    assert non_photographable_reason(brief), f"{label} would still reach image search"


# --- Arabic ---------------------------------------------------------------

@pytest.mark.parametrize(
    "label,brief",
    [
        ("rasm bayani", AR_CHART_MONEY),
        ("mukhattat", "مخطط تكتيكي لتشكيل الفريق"),
        ("rasm tawdihi", "رسم توضيحي لقواعد التسلل"),
        ("domino", "تأثير الدومينو على أسعار اللاعبين"),
        ("infographic", "إنفوجرافيك أرقام الموسم"),
        ("jadwal bayani", "جدول بياني لترتيب الدوري"),
    ],
)
def test_arabic_diagram_briefs_are_caught(label, brief):
    assert non_photographable_reason(brief), f"Arabic {label} not detected"
    assert conceptual_brief_reason(brief), f"Arabic {label} not routed to a component"


def test_arabic_spacing_variants_are_caught():
    """The scripter does not write consistent spacing."""
    for brief in ("رسم بياني للأهداف", "رسم  بياني  للأهداف"):
        assert non_photographable_reason(brief), f"{brief!r} not detected"


# --- English --------------------------------------------------------------

@pytest.mark.parametrize(
    "label,brief",
    [
        ("diagram", "diagram of cascading falling arrows"),
        ("chart", "chart showing money flow between clubs"),
        ("infographic", "infographic chart showing money flow"),
        ("info-graphic", "info-graphic of transfer spending"),
        ("graph", "graph of rising transfer fees"),
        ("domino effect", "domino effect of club badges falling"),
        ("flow chart", "flow chart of the transfer process"),
        ("flowchart", "flowchart of the appeals process"),
        ("bar chart", "bar chart of goals per season"),
        ("pie chart", "pie chart of squad nationalities"),
        ("timeline", "timeline of the club's history"),
        ("ripple effect", "ripple effect across the league"),
        ("chain reaction", "chain reaction of transfers"),
        ("venn diagram", "venn diagram of overlapping squads"),
        ("mind map", "mind map of the tactical system"),
    ],
)
def test_english_diagram_briefs_are_caught(label, brief):
    assert non_photographable_reason(brief), f"English {label} not detected"
    assert conceptual_brief_reason(brief), f"English {label} not routed to a component"


# --- what must still be allowed -------------------------------------------

@pytest.mark.parametrize(
    "brief",
    [
        "ملعب سانتياغو برنابيو صورة فوتوغرافية",
        "ملعب نادي لوتون تاون كينيلورث رود صورة فوتوغرافية",
        "جماهير كرة القدم تحتفل بهدف في الملعب",
        "Santiago Bernabeu stadium exterior",
        "football fans celebrating in the stands",
        "a young footballer training at a small academy",
        "an empty wallet on a wooden table",
    ],
)
def test_real_subjects_are_still_sourceable(brief):
    assert non_photographable_reason(brief) == "", f"blocked a real subject: {brief!r}"
    assert conceptual_brief_reason(brief) == ""


def test_an_archival_document_still_outranks_the_new_patterns():
    """A genuine case record named as one stays allowed, as before."""
    for brief in (
        "FBI evidence document of the flight path chart",
        "newspaper clipping about the transfer",
        "police file diagram of the scene",
    ):
        assert conceptual_brief_reason(brief) == "", f"blocked a record: {brief!r}"


def test_existing_graphic_device_rules_are_unchanged():
    """The earlier tiers must keep behaving exactly as they did."""
    assert non_photographable_reason("question mark D.B. Cooper composite sketch")
    assert non_photographable_reason("cinematic mountain ridge")
    assert non_photographable_reason("artist's impression of the aircraft")
    assert non_photographable_reason("FBI composite sketch of the suspect") == ""


def test_an_empty_brief_is_not_an_error():
    assert non_photographable_reason("") == ""
    assert conceptual_brief_reason("") == ""
    assert conceptual_brief_reason("   ") == ""


# --- the remedy the scripter is given -------------------------------------

def _validate(keywords: str, *, web_photos_only: bool) -> list[str]:
    from core.scripter import _script_validation_errors
    from core.utils import ScriptTimingProfile

    data = {
        "title": "t",
        "sections": [{
            "id": 1,
            "narration": "n",
            "estimated_duration_seconds": 10,
            "slots": [{"visual": "google_photo", "keywords": keywords}],
        }],
    }
    errors, _ = _script_validation_errors(
        data,
        numbering_order=None,
        max_visual_hold_seconds=5.0,
        crossfade=0.3,
        timing_profile=ScriptTimingProfile(
            model_name="test",
            words_per_minute=150.0,
            section_overhead_seconds=1.0,
        ),
        web_photos_only=web_photos_only,
    )
    return errors


def test_the_validator_tells_the_scripter_to_change_the_slot_type():
    """"Find a more concrete subject" is the wrong advice for a bar chart."""
    errors = _validate(AR_CHART_MONEY, web_photos_only=True)
    conceptual = [e for e in errors if "رسم بياني" in e]
    assert conceptual, f"the diagram slot produced no validation error: {errors}"
    message = conceptual[0]
    assert "info_card" in message and "info_slide" in message
    assert "renderer draws" in message


def test_the_two_remedies_are_not_both_emitted():
    """One slot, one instruction -- contradictory advice is worse than none."""
    errors = _validate("infographic chart of spending", web_photos_only=True)
    slot_errors = [e for e in errors if "infographic chart" in e]
    assert len(slot_errors) == 1, f"expected one instruction, got {slot_errors}"


def test_channels_that_allow_illustration_are_untouched():
    """Only web_photos_only channels get this validation at all."""
    errors = _validate("infographic chart of spending", web_photos_only=False)
    assert not [e for e in errors if "infographic" in e]
