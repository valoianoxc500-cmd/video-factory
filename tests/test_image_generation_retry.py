"""Image generation must back off on a 429 like every other model call.

Observed in a real run: four generations failed in the same second with no
wait between them, because this path called the SDK directly instead of going
through the retry ladder. The beats kept their rejected stock images and the
review gate then failed the run.

Image quota is the tightest of any model used here, so it is the path that
most needs the wait.
"""

from __future__ import annotations

import asyncio

import pytest

import clients


def _run(coro):
    return asyncio.run(coro)


def test_a_429_is_retried_rather_than_failing_the_beat(monkeypatch):
    attempts = {"n": 0}

    async def flaky():
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise RuntimeError("429 RESOURCE_EXHAUSTED")
        return "ok"

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(clients.asyncio, "sleep", no_sleep)
    assert _run(clients._call_model_with_retry("generated_fallback", flaky)) == "ok"
    assert attempts["n"] == 3


def test_the_wait_grows_between_attempts(monkeypatch):
    waits: list[float] = []

    async def always_429():
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    async def record(seconds):
        waits.append(seconds)

    monkeypatch.setattr(clients.asyncio, "sleep", record)
    with pytest.raises(RuntimeError):
        _run(clients._call_model_with_retry("generated_fallback", always_429))

    assert waits == sorted(waits), "backoff did not grow"
    assert waits[0] >= clients._MODEL_CALL_BASE_DELAY
    assert max(waits) <= clients._MODEL_CALL_MAX_DELAY


def test_a_deterministic_failure_is_not_retried(monkeypatch):
    """Retrying an invalid prompt spends quota to get the same answer."""
    attempts = {"n": 0}

    async def invalid():
        attempts["n"] += 1
        raise ValueError("400 INVALID_ARGUMENT: prompt was rejected")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(clients.asyncio, "sleep", no_sleep)
    with pytest.raises(ValueError):
        _run(clients._call_model_with_retry("generated_fallback", invalid))
    assert attempts["n"] == 1


def test_retries_are_bounded(monkeypatch):
    attempts = {"n": 0}

    async def always_429():
        attempts["n"] += 1
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(clients.asyncio, "sleep", no_sleep)
    with pytest.raises(RuntimeError):
        _run(clients._call_model_with_retry("generated_fallback", always_429))
    assert attempts["n"] == clients._MODEL_CALL_MAX_ATTEMPTS


def test_image_generation_goes_through_the_ladder():
    """The regression itself: this path called the SDK directly."""
    import inspect

    source = inspect.getsource(clients.generate_image_gemini)
    assert "_call_model_with_retry" in source, (
        "image generation bypasses the retry ladder, so a 429 fails the beat "
        "with no backoff"
    )


@pytest.mark.parametrize("marker", [
    "429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "INTERNAL",
    "504", "DEADLINE_EXCEEDED",
])
def test_transient_markers_are_recognised(marker):
    assert clients._is_retryable_model_error(RuntimeError(marker)) is True


def test_an_ordinary_error_is_not_transient():
    assert clients._is_retryable_model_error(ValueError("bad schema")) is False
