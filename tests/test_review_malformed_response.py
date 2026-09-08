"""A malformed reviewer response is not a verdict.

Reviewing seventeen images produces a long JSON body. When it is truncated the
decoder raises "Unterminated string", and that was being converted into
`approved: False` with no per-image detail -- which spent one of the gate's two
content attempts and left the regenerate step nothing to target. A run could
fail the relevance gate without the reviewer ever having judged the images.

Re-asking is not a weakening: a parse failure never approves anything, and a
genuine rejection still falls straight through on the first response.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from core import reviewer
from core.reviewer import _MALFORMED_REVIEW_RETRIES, ReviewGateError, review_gate


def _run(coro):
    return asyncio.run(coro)


def _gate(monkeypatch, responses, *, max_attempts=2, regenerate=None):
    """Drive the gate with a scripted sequence of reviewer outcomes."""
    calls = {"review": 0, "regenerate": 0}

    async def fake_review(prompt, image_paths, **kwargs):
        index = min(calls["review"], len(responses) - 1)
        calls["review"] += 1
        outcome = responses[index]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(reviewer.clients, "review_with_vision", fake_review)

    async def fake_regenerate(content, feedback):
        calls["regenerate"] += 1
        return content

    return calls, fake_regenerate if regenerate is None else regenerate


APPROVED = {"approved": True, "feedback": "all good"}
REJECTED = {"approved": False, "feedback": "image 1 shows the wrong subject"}


def _call(monkeypatch, responses, *, max_attempts=2):
    calls, regen = _gate(monkeypatch, responses)
    try:
        result = _run(review_gate(
            content={"x": 1},
            review_prompt_fn=lambda c: "prompt",
            regenerate_fn=regen,
            image_paths=["a.jpg"],
            system_instruction="sys",
            max_attempts=max_attempts,
            gate_name="image_review",
        ))
        return calls, result, None
    except ReviewGateError as exc:
        return calls, None, exc


# --- the fix ---------------------------------------------------------------

def test_a_truncated_response_is_re_asked_not_counted_as_a_rejection(monkeypatch):
    truncated = json.JSONDecodeError("Unterminated string", "{...", 10)
    calls, result, error = _call(monkeypatch, [truncated, APPROVED])

    assert error is None, "a parse failure failed the gate"
    assert result is not None
    # Two reviewer calls, but the content was never regenerated: the first
    # response was not a verdict about the images.
    assert calls["review"] == 2
    assert calls["regenerate"] == 0


def test_re_asking_is_bounded(monkeypatch):
    """A persistently broken reviewer still fails rather than looping."""
    truncated = json.JSONDecodeError("Unterminated string", "{...", 10)
    calls, _, error = _call(monkeypatch, [truncated], max_attempts=1)

    assert error is not None
    assert calls["review"] == _MALFORMED_REVIEW_RETRIES


def test_a_genuine_rejection_still_costs_an_attempt(monkeypatch):
    """The gate must be exactly as strict as before about content."""
    calls, _, error = _call(monkeypatch, [REJECTED], max_attempts=2)

    assert error is not None, "a rejected image passed the gate"
    assert calls["regenerate"] == 1, "the gate did not try to fix the content"


def test_a_rejection_is_never_re_asked(monkeypatch):
    """Re-asking a verdict would be shopping for a better answer."""
    calls, _, _ = _call(monkeypatch, [REJECTED], max_attempts=1)
    assert calls["review"] == 1


def test_an_approval_is_returned_immediately(monkeypatch):
    calls, result, error = _call(monkeypatch, [APPROVED])
    assert error is None and result is not None
    assert calls["review"] == 1


def test_a_transport_error_is_not_re_asked_as_a_parse_error(monkeypatch):
    """A 500 is a different failure and keeps the old single-shot behaviour."""
    calls, _, error = _call(monkeypatch, [RuntimeError("503 unavailable")],
                            max_attempts=1)
    assert error is not None
    assert calls["review"] == 1


def test_a_parse_failure_never_approves(monkeypatch):
    """The safety property: broken JSON cannot become a pass."""
    truncated = json.JSONDecodeError("Unterminated string", "{...", 10)
    calls, result, error = _call(monkeypatch, [truncated], max_attempts=1)
    assert result is None
    assert error is not None
