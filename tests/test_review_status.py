"""Review verdicts must not label a good video "Flagged" on a bad judgement.

Two real verdicts drive these tests:

* Flight 19 -- the reviewer listed as critical that the caption highlight was
  "#FF0000" rather than the configured "#e23b3b". Sampling the rendered frames
  put it at #E23A3B in 27 of 27 samples. The claim was simply wrong, and it
  flagged an otherwise-sound video.

* The cannibalism story -- watermarks, an elderly man where the narration says
  a woman, and vintage-advert filler. Those are real, and must stay flagged.
"""

from __future__ import annotations

import pytest

from core.review_status import (
    STATUS_APPROVED,
    STATUS_NOTES,
    classify_gate,
    classify_review_log,
    is_unreliable_finding,
)

COLOUR_CLAIM = (
    "Caption style: The red highlight color used does not match the specific "
    "hex code #e23b3b required by the brand guidelines; it appears as a "
    "standard saturated red (#FF0000)."
)

REAL_FINDINGS = [
    "Image 3 contains a visible 'Adobe Stock' watermark, which is "
    "unprofessional for a final product.",
    "Image 3 shows an elderly man while the narration specifically refers to "
    "a woman.",
    "Images 11 and 12 consist of a collage of vintage advertisements that are "
    "generic filler and completely irrelevant to a horror story.",
    "Frames 4 and 7 show a radio receiver with 'AIR FRANCE' branding, which is "
    "historically inaccurate for a video about a US Navy mission.",
    "The narration claims a death toll the research brief does not support.",
]


# --- what counts as an unreliable judgement --------------------------------

def test_the_measured_false_positive_is_recognised():
    assert is_unreliable_finding(COLOUR_CLAIM) is True


@pytest.mark.parametrize("finding", REAL_FINDINGS)
def test_real_findings_are_never_discounted(finding):
    assert is_unreliable_finding(finding) is False, finding


def test_a_colour_claim_that_also_reports_a_real_defect_is_kept():
    """Mentioning a colour must not launder a genuine problem."""
    finding = (
        "The subtitle text is #e23b3b but a large red Adobe Stock watermark "
        "covers the frame."
    )
    assert is_unreliable_finding(finding) is False


def test_a_colour_word_in_an_unrelated_finding_is_kept():
    assert is_unreliable_finding(
        "The red car in image 4 is unrelated to the narration."
    ) is False


def test_empty_findings_are_not_discounted():
    assert is_unreliable_finding("") is False
    assert is_unreliable_finding(None) is False


# --- gate-level grading ----------------------------------------------------

def _final_review(critical, minor=None):
    return {
        "approved": False,
        "flagged": True,
        "attempts": 1,
        "review_history": [
            {"attempt": 1, "approved": False,
             "critical_issues": list(critical),
             "minor_issues": list(minor or [])},
        ],
    }


def test_a_gate_whose_only_critical_finding_is_the_colour_claim_is_not_flagged():
    """The exact Flight 19 regression."""
    graded = classify_gate("final_review", _final_review([COLOUR_CLAIM]))
    assert graded["flagged"] is False
    assert graded["discounted"] == [COLOUR_CLAIM]
    assert graded["critical"] == []


def test_a_gate_with_one_real_finding_stays_flagged():
    graded = classify_gate("final_review", _final_review([REAL_FINDINGS[0]]))
    assert graded["flagged"] is True
    assert graded["critical"] == [REAL_FINDINGS[0]]


def test_a_real_finding_alongside_the_colour_claim_stays_flagged():
    """Discounting one bad judgement must not discard the others."""
    graded = classify_gate(
        "final_review", _final_review([COLOUR_CLAIM, REAL_FINDINGS[1]])
    )
    assert graded["flagged"] is True
    assert graded["critical"] == [REAL_FINDINGS[1]]
    assert graded["discounted"] == [COLOUR_CLAIM]


def test_minor_issues_alone_do_not_flag():
    graded = classify_gate("final_review", _final_review([], ["slight style drift"]))
    assert graded["flagged"] is False
    assert graded["minor"] == ["slight style drift"]


def test_image_review_errors_are_critical_and_warnings_are_not():
    entry = {
        "approved": False, "flagged": True, "attempts": 2,
        "review_history": [{
            "attempt": 1, "approved": False,
            "image_results": [
                {"section_id": 1, "sub_image_index": 2, "severity": "warning",
                 "approved": True, "issues": ["watermark visible"]},
                {"section_id": 2, "sub_image_index": 1, "severity": "error",
                 "approved": False, "failure_type": "wrong_subject",
                 "issues": ["The image does not show a hand holding a hammer."]},
            ],
        }],
    }
    graded = classify_gate("image_review", entry)
    assert graded["flagged"] is True
    assert len(graded["critical"]) == 1
    assert "hammer" in graded["critical"][0]
    assert len(graded["minor"]) == 1


def test_only_warning_severity_images_do_not_flag():
    entry = {
        "approved": False, "flagged": True, "attempts": 1,
        "review_history": [{
            "attempt": 1, "approved": False,
            "image_results": [
                {"section_id": 1, "sub_image_index": 2, "severity": "warning",
                 "approved": True, "issues": ["watermark visible"]},
            ],
        }],
    }
    assert classify_gate("image_review", entry)["flagged"] is False


def test_a_gate_with_no_structured_findings_is_trusted():
    """Unknown verdict shapes must fail safe: keep the gate's own answer."""
    entry = {"approved": False, "flagged": True, "attempts": 1,
             "review_history": [{"attempt": 1, "approved": False}]}
    assert classify_gate("final_review", entry)["flagged"] is True


def test_an_approved_gate_is_not_flagged():
    entry = {"approved": True, "flagged": False, "attempts": 1,
             "review_history": [{"attempt": 1, "approved": True}]}
    assert classify_gate("script_review", entry)["flagged"] is False


# --- whole-log status ------------------------------------------------------

def test_a_clean_run_is_approved():
    log = {
        "script_review": {"approved": True, "flagged": False},
        "final_review": {"approved": True, "flagged": False},
    }
    status, _ = classify_review_log(log)
    assert status == STATUS_APPROVED


def test_the_flight_19_colour_verdict_no_longer_flags_the_video():
    status, detail = classify_review_log({
        "script_review": {"approved": True, "flagged": False},
        "final_review": _final_review([COLOUR_CLAIM]),
    })
    assert not status.startswith("flagged")
    assert status == STATUS_NOTES
    assert detail["discounted_count"] == 1


def test_the_cannibalism_verdict_stays_flagged():
    """Real defects must survive the fix."""
    status, _ = classify_review_log({
        "final_review": _final_review(REAL_FINDINGS[:3]),
    })
    assert status.startswith("flagged")
    assert "final_review" in status


def test_multiple_flagged_gates_are_all_named():
    status, _ = classify_review_log({
        "image_review": _final_review([REAL_FINDINGS[0]]),
        "final_review": _final_review([REAL_FINDINGS[1]]),
    })
    assert status == "flagged: final_review, image_review"


def test_safety_findings_are_never_discounted():
    """The check that must not be weakened by any of this."""
    for unsafe in (
        "The narration contains graphic gore inappropriate for the platform.",
        "Image 5 shows an unsafe depiction of a real victim.",
        "The video presents a fabricated document as a real photograph.",
    ):
        assert is_unreliable_finding(unsafe) is False
        status, _ = classify_review_log({"final_review": _final_review([unsafe])})
        assert status.startswith("flagged"), unsafe


def test_an_empty_log_is_approved():
    status, detail = classify_review_log({})
    assert status == STATUS_APPROVED
    assert detail["gates"] == []
