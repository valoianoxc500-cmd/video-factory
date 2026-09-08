"""A rejection that names no image is malformed, not actionable.

Observed: the reviewer returned one rejected image with no section_id or
sub_image_index. `_rejected_slot_keys` skipped it, the gate reported "no
per-image results", and both review attempts were spent having re-sourced
nothing.

The fix treats it the way a truncated body is treated -- re-ask. It
deliberately does not infer the image from its position: the reviewer numbers
what it was shown, and that numbering shifts whenever a slot has no asset, so
position identifies the wrong beat.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core import reviewer
from core.reviewer import (
    _MALFORMED_REVIEW_RETRIES,
    ReviewGateError,
    require_slot_identity,
    review_gate,
)


def _run(coro):
    return asyncio.run(coro)


def _result(section_id=1, sub=1, approved=False, **over):
    item = {"approved": approved, "severity": "error" if not approved else "ok"}
    if section_id is not None:
        item["section_id"] = section_id
    if sub is not None:
        item["sub_image_index"] = sub
    item.update(over)
    return item


# --- the check itself -------------------------------------------------------

def test_an_identified_rejection_passes():
    require_slot_identity(
        {"image_results": [_result(1, 3)]}, "image_review")


def test_a_rejection_without_a_section_is_malformed():
    with pytest.raises(ValueError, match="section_id/sub_image_index"):
        require_slot_identity(
            {"image_results": [_result(section_id=None)]}, "image_review")


def test_a_rejection_without_a_sub_index_is_malformed():
    with pytest.raises(ValueError):
        require_slot_identity(
            {"image_results": [_result(sub=None)]}, "image_review")


def test_a_non_numeric_identity_is_malformed():
    with pytest.raises(ValueError):
        require_slot_identity(
            {"image_results": [_result(section_id="the second one")]},
            "image_review")


def test_an_approved_image_needs_no_identity():
    """Only rejections have to be actionable."""
    require_slot_identity(
        {"image_results": [_result(section_id=None, approved=True)]},
        "image_review")


def test_the_error_says_how_many_were_unusable():
    with pytest.raises(ValueError, match="2 rejected image"):
        require_slot_identity(
            {"image_results": [_result(section_id=None),
                               _result(sub=None),
                               _result(1, 1)]},
            "image_review")


def test_other_gates_are_untouched():
    """Only image_review carries per-image results."""
    require_slot_identity({"image_results": [_result(section_id=None)]},
                          "final_review")
    require_slot_identity("not a dict", "image_review")


# --- it re-asks rather than acting ------------------------------------------

APPROVED = {"approved": True, "feedback": "fine"}
UNIDENTIFIED = {
    "approved": False,
    "feedback": "Raw list response: 1 rejected image(s)",
    "image_results": [_result(section_id=None)],
}
IDENTIFIED = {
    "approved": False,
    "feedback": "one wrong subject",
    "image_results": [_result(1, 3, suggestion="Search for 'a desk' instead")],
}


def _drive(monkeypatch, responses, *, max_attempts=2):
    calls = {"review": 0, "regenerate": 0}

    async def fake_review(prompt, image_paths, **kwargs):
        index = min(calls["review"], len(responses) - 1)
        calls["review"] += 1
        outcome = responses[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def fake_regenerate(content, feedback):
        calls["regenerate"] += 1
        return content

    monkeypatch.setattr(reviewer.clients, "review_with_vision", fake_review)

    try:
        result = _run(review_gate(
            content={"x": 1},
            review_prompt_fn=lambda c: "prompt",
            regenerate_fn=fake_regenerate,
            image_paths=["a.jpg"],
            system_instruction="sys",
            max_attempts=max_attempts,
            gate_name="image_review",
        ))
        return calls, result, None
    except ReviewGateError as exc:
        return calls, None, exc


def test_an_unidentified_rejection_is_re_asked(monkeypatch):
    """And the second answer is acted on, without spending a content attempt."""
    calls, result, error = _drive(monkeypatch, [UNIDENTIFIED, APPROVED])

    assert error is None
    assert calls["review"] == 2
    assert calls["regenerate"] == 0, "an unusable response triggered a rewrite"


def test_re_asking_is_bounded(monkeypatch):
    calls, _, error = _drive(monkeypatch, [UNIDENTIFIED], max_attempts=1)

    assert error is not None
    assert calls["review"] == _MALFORMED_REVIEW_RETRIES


def test_an_identified_rejection_is_acted_on_immediately(monkeypatch):
    """The normal path must not become slower or more expensive."""
    calls, _, error = _drive(monkeypatch, [IDENTIFIED], max_attempts=2)

    assert error is not None
    assert calls["regenerate"] == 1, "an actionable rejection was not retried"


def test_an_approval_still_returns_first_time(monkeypatch):
    calls, result, error = _drive(monkeypatch, [APPROVED])
    assert error is None and calls["review"] == 1


def test_a_raw_list_is_normalised_before_the_check(monkeypatch):
    """The shape that caused the bug: a bare list from the model."""
    raw = [_result(1, 3, suggestion="Search for 'a desk' instead")]
    calls, _, error = _drive(monkeypatch, [raw], max_attempts=1)

    # It was understood as one identified rejection, so the gate failed on
    # content rather than re-asking three times.
    assert calls["review"] == 1
    assert error is not None


def test_a_raw_list_without_identity_is_re_asked(monkeypatch):
    raw = [_result(section_id=None)]
    calls, _, _ = _drive(monkeypatch, [raw], max_attempts=1)
    assert calls["review"] == _MALFORMED_REVIEW_RETRIES


def test_identity_is_never_inferred_from_position():
    """The rule that keeps the wrong beat from being regenerated."""
    import inspect

    source = inspect.getsource(require_slot_identity)
    assert "enumerate" not in source
    assert "index" not in source.replace("sub_image_index", "")


def test_the_prompt_demands_the_fields():
    import prompts

    schema = prompts.image_review_prompt(
        sections_context=[{
            "section_id": 1, "sub_image_index": 1, "narration": "n",
            "visual_type": "stock_photo", "is_section_opener": True,
            "text_only": False, "prompt": "p", "image_search_keywords": "k",
            "image_filename": "section_001_01.jpg",
        }],
    )
    assert "mandatory on every entry" in schema
    assert "REQUIRED on every entry" in schema
