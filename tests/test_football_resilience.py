"""Football resilience: no quota storms, and citations beat memory.

Both behaviours here come from one real production run
(99db2deb-4a16-4467-b318-a01fba131b7e, "Real Madrid Vs Inter Milan recent
game"). It failed twice for two different avoidable reasons:

  * 29 Gemini calls in five minutes, every one refused with 429, because the
    per-call retry ladder is patient and image sourcing runs many beats at
    once. Each caller was correct; the aggregate was a storm.

  * the script reviewer rejected the script for saying the match was on
    8 September 2026 -- the date the retrieved ESPN report gives -- because it
    remembered a 2021 fixture between the same clubs.

Nothing here lowers a factual threshold. The breaker changes how long callers
wait, not whether a bad answer is accepted; the grounded block tells the
reviewer which facts are cited, and explicitly keeps it rejecting anything
those citations do not support.
"""

import asyncio

import pytest

import clients
import prompts


@pytest.fixture(autouse=True)
def closed_circuit():
    clients.reset_quota_circuit()
    yield
    clients.reset_quota_circuit()


def _no_sleep(monkeypatch):
    """Make backoff instant without recursing into the patched sleep.

    Binding the real coroutine first matters. A lambda that calls
    `asyncio.sleep` after the patch calls *itself*, and the resulting
    RecursionError is a RuntimeError subclass -- so it gets caught by the very
    `pytest.raises(RuntimeError)` these tests use, and the retry loop never
    actually runs. That is how the first version of this file "passed" three
    calls without the breaker ever seeing eight failures.
    """
    real_sleep = asyncio.sleep
    monkeypatch.setattr(clients.asyncio, "sleep", lambda _: real_sleep(0))


def _quota_error():
    return RuntimeError(
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, "
        "'message': 'Resource exhausted. Please try again later.'}}"
    )


# ── the breaker ──────────────────────────────────────────────────────

def test_the_circuit_starts_closed():
    assert clients.quota_circuit_open() is False


def test_repeated_quota_refusals_trip_the_breaker(monkeypatch):
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    async def always_429():
        calls["n"] += 1
        raise _quota_error()

    # Failures are counted per *call*, not per attempt, so a single caller's
    # backoff ladder is never cut short. It takes several refused calls.
    for _ in range(clients._QUOTA_TRIP_THRESHOLD):
        with pytest.raises(RuntimeError):
            asyncio.run(clients._call_model_with_retry("serper_candidate_selection", always_429))

    assert clients.quota_circuit_open() is True, "the breaker never tripped"


def test_an_open_breaker_stops_the_storm(monkeypatch):
    """The behaviour that matters: far fewer calls once quota is exhausted."""
    _no_sleep(monkeypatch)
    calls = {"n": 0}

    async def always_429():
        calls["n"] += 1
        raise _quota_error()

    # Twenty beats each asking for a candidate selection, as image sourcing
    # does when a whole script's visuals are sourced concurrently.
    for _ in range(20):
        with pytest.raises(RuntimeError):
            asyncio.run(clients._call_model_with_retry("open_library_candidate_selection", always_429))
    assert clients.quota_circuit_open() is True

    # Unbounded this is 20 x 8 = 160 attempts. Once the breaker opens each
    # further caller costs exactly one, so the total is bounded by the
    # threshold rather than by how many beats the script happens to have.
    ceiling = clients._QUOTA_TRIP_THRESHOLD * clients._MODEL_CALL_MAX_ATTEMPTS + 20
    assert calls["n"] <= ceiling, f"still a storm: {calls['n']} calls"

    # And the marginal cost of the next beat is one call, not eight.
    before = calls["n"]
    with pytest.raises(RuntimeError):
        asyncio.run(clients._call_model_with_retry("open_library_candidate_selection", always_429))
    assert calls["n"] - before == 1, "an open breaker still let a caller retry"


def test_the_caller_still_gets_its_error(monkeypatch):
    """The breaker must not swallow a failure into a fake success."""
    _no_sleep(monkeypatch)

    async def always_429():
        raise _quota_error()

    for _ in range(10):
        with pytest.raises(RuntimeError):
            asyncio.run(clients._call_model_with_retry("x", always_429))


def test_a_success_closes_the_breaker(monkeypatch):
    _no_sleep(monkeypatch)
    state = {"fail": True}

    async def flaky():
        if state["fail"]:
            raise _quota_error()
        return "ok"

    for _ in range(10):
        with pytest.raises(RuntimeError):
            asyncio.run(clients._call_model_with_retry("x", flaky))
    assert clients.quota_circuit_open() is True

    state["fail"] = False
    clients.reset_quota_circuit()
    assert asyncio.run(clients._call_model_with_retry("x", flaky)) == "ok"
    assert clients.quota_circuit_open() is False


def test_a_non_quota_error_does_not_trip_the_breaker(monkeypatch):
    """A 500 is worth waiting out; it must not disable Gemini for the run."""
    _no_sleep(monkeypatch)

    async def server_error():
        raise RuntimeError("500 INTERNAL")

    for _ in range(10):
        with pytest.raises(RuntimeError):
            asyncio.run(clients._call_model_with_retry("x", server_error))
    assert clients.quota_circuit_open() is False


def test_a_non_retryable_error_is_raised_immediately(monkeypatch):
    calls = {"n": 0}

    async def bad_request():
        calls["n"] += 1
        raise ValueError("400 INVALID_ARGUMENT")

    with pytest.raises(ValueError):
        asyncio.run(clients._call_model_with_retry("x", bad_request))
    assert calls["n"] == 1, "a permanent error was retried"


# ── grounded facts beat the reviewer's memory ────────────────────────

BRIEF = (
    "- [espn.com, 2026-09-08, 3d ago] Real Madrid 2-1 Inter Milan "
    "(Sep 8, 2026) Game Analysis"
)


def test_the_reviewer_is_given_the_grounded_facts():
    prompt = prompts.script_review_prompt("{}", grounded_brief=BRIEF)
    assert "Real Madrid 2-1 Inter Milan" in prompt
    assert "VERIFIED FACTS FOR THIS RUN" in prompt


def test_the_reviewer_is_told_citations_outrank_its_memory():
    prompt = prompts.script_review_prompt("{}", grounded_brief=BRIEF).lower()
    assert "your prior is" in prompt or "not your prior" in prompt
    # The specific failure: rejecting a visual because the date disagrees.
    assert "do not reject a visual prompt" in prompt


def test_the_reviewer_must_still_reject_unsupported_claims():
    """Elevating cited facts must not become a blanket pass."""
    prompt = prompts.script_review_prompt("{}", grounded_brief=BRIEF)
    assert "must still reject" in prompt
    for unsupported in ("invented statistic", "unsupported quote"):
        assert unsupported in prompt


def test_no_grounded_block_when_research_found_nothing():
    """An ungrounded run must not get an empty 'verified facts' section."""
    for empty in ("", "   ", "\n"):
        prompt = prompts.script_review_prompt("{}", grounded_brief=empty)
        assert "VERIFIED FACTS FOR THIS RUN" not in prompt


def test_the_grounded_brief_comes_from_research_not_the_user():
    """Only the grounding layer's output is elevated, never raw user text."""
    import inspect
    from core import scripter

    source = inspect.getsource(scripter)
    assert "grounded_brief=research_context" in source
    # `research_context` is the plan's researched field, not the topic.
    assert 'research_context = plan.get("research_context"' in source
    assert "grounded_brief=topic" not in source
