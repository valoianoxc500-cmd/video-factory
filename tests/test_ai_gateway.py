"""The AI gateway: routing, health, coalescing, budget, fallback, escalation.

The rule these tests exist to protect is that turning a flag on may change
*which* model answers, but never whether the call happens at all. Every
uncertain path falls back to the model the call site already asked for.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from core import ai_gateway as gw
from core.ai_gateway import (
    Budget,
    Capability,
    GatewayMode,
    Health,
    HealthTracker,
    ModelPolicy,
    ModelSpec,
    SingleFlight,
    Tier,
    cache_key,
    execute,
    is_rate_limited,
    metrics,
    omniroute_available,
    omniroute_generate,
    route,
)


def _run(coro):
    return asyncio.run(coro)


CHEAP = ModelSpec("cheap-1", Tier.CHEAP,
                  frozenset({Capability.TEXT, Capability.JSON}), 0.1)
CHEAP_VISION = ModelSpec("cheap-vision", Tier.CHEAP,
                         frozenset({Capability.TEXT, Capability.JSON,
                                    Capability.VISION}), 0.2)
BALANCED = ModelSpec("balanced-1", Tier.BALANCED,
                     frozenset({Capability.TEXT, Capability.JSON,
                                Capability.VISION}), 0.4)
STRONG = ModelSpec("strong-1", Tier.STRONG,
                   frozenset({Capability.TEXT, Capability.JSON,
                              Capability.VISION}), 1.0)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """Each test starts with clean health, no in-flight calls, routing on."""
    gw.reset_state()
    gw.set_policy(ModelPolicy([CHEAP, CHEAP_VISION, BALANCED, STRONG]))
    monkeypatch.setenv("AI_SMART_ROUTING_ENABLED", "true")
    monkeypatch.setenv("AI_CACHE_ENABLED", "true")
    monkeypatch.setenv("AI_REQUEST_COALESCING_ENABLED", "true")
    monkeypatch.setenv("AI_GATEWAY_MODE", "direct")
    yield
    gw.set_policy(None)
    gw.reset_state()


# --- routing by task --------------------------------------------------------

def test_a_cheap_task_gets_a_cheap_model():
    assert route("keyword_extraction", requested="strong-1") == "cheap-1"


def test_a_strong_task_is_not_downgraded():
    """Cost routing must not answer a fact check with the cheapest model."""
    assert route("fact_check", requested="cheap-1") == "strong-1"


def test_a_task_needing_vision_never_gets_a_model_without_it():
    chosen = route("image_relevance", requested="")
    spec = next(m for m in gw.policy().catalogue if m.name == chosen)
    assert Capability.VISION in spec.capabilities


def test_an_unknown_task_keeps_the_callers_own_model():
    assert route("something_new", requested="caller-model") == "caller-model"


def test_routing_off_keeps_the_callers_own_model(monkeypatch):
    monkeypatch.setenv("AI_SMART_ROUTING_ENABLED", "false")
    assert route("keyword_extraction", requested="caller-model") == "caller-model"


def test_the_cheapest_qualifying_model_wins():
    policy = ModelPolicy([STRONG, BALANCED, CHEAP])
    gw.set_policy(policy)
    assert policy.choose("keyword_extraction") == "cheap-1"


def test_escalation_walks_upward():
    policy = gw.policy()
    assert policy.escalate("keyword_extraction", "cheap-1") in {
        "cheap-vision", "balanced-1"}
    assert policy.escalate("fact_check", "strong-1") == ""


# --- health -----------------------------------------------------------------

def test_a_rate_limited_model_goes_into_cooldown():
    clock = {"t": 1000.0}
    health = HealthTracker(clock=lambda: clock["t"])
    health.record_failure("cheap-1", rate_limited=True)

    assert health.state("cheap-1") is Health.COOLDOWN
    assert health.usable("cheap-1") is False

    clock["t"] += HealthTracker.COOLDOWN_SECONDS + 1
    assert health.usable("cheap-1") is True


def test_a_model_in_cooldown_is_skipped_for_the_next_request():
    """The point of the cooldown: stop feeding a model that said slow down."""
    gw.HEALTH.record_failure("cheap-1", rate_limited=True)
    assert route("keyword_extraction", requested="") != "cheap-1"


def test_success_clears_a_degraded_model():
    gw.HEALTH.record_failure("cheap-1")
    gw.HEALTH.record_failure("cheap-1")
    assert gw.HEALTH.state("cheap-1") is Health.DEGRADED
    gw.HEALTH.record_success("cheap-1")
    assert gw.HEALTH.state("cheap-1") is Health.HEALTHY


def test_every_model_in_cooldown_falls_back_to_the_caller():
    for spec in gw.policy().catalogue:
        gw.HEALTH.record_failure(spec.name, rate_limited=True)
    assert route("keyword_extraction", requested="caller") == "caller"


@pytest.mark.parametrize("message", [
    "429 Too Many Requests", "RESOURCE_EXHAUSTED", "rate limit exceeded",
])
def test_rate_limit_errors_are_recognised(message):
    assert is_rate_limited(RuntimeError(message)) is True


def test_an_ordinary_error_is_not_a_rate_limit():
    assert is_rate_limited(ValueError("bad schema")) is False


# --- executing --------------------------------------------------------------

def test_a_successful_call_returns_its_result():
    async def call(model):
        return {"model": model}

    result = _run(execute("keyword_extraction", call, requested="x"))
    assert result["model"] == "cheap-1"


def test_a_failure_falls_back_to_the_next_model():
    seen: list[str] = []

    async def call(model):
        seen.append(model)
        if model == "cheap-1":
            raise RuntimeError("429 rate limited")
        return "ok"

    assert _run(execute("keyword_extraction", call)) == "ok"
    assert seen[0] == "cheap-1" and len(seen) > 1


def test_the_failed_model_is_marked_unhealthy():
    async def call(model):
        if model == "cheap-1":
            raise RuntimeError("429")
        return "ok"

    _run(execute("keyword_extraction", call))
    assert gw.HEALTH.usable("cheap-1") is False


def test_the_last_model_failing_raises_rather_than_inventing_an_answer():
    async def call(model):
        raise RuntimeError("everything is down")

    with pytest.raises(RuntimeError, match="everything is down"):
        _run(execute("fact_check", call))


def test_a_poor_answer_is_escalated_once():
    seen: list[str] = []

    async def call(model):
        seen.append(model)
        return "short" if model == "cheap-1" else "a proper answer"

    result = _run(execute(
        "keyword_extraction", call,
        validator=lambda value: len(value) > 10))

    assert result == "a proper answer"
    assert len(seen) == 2, "the cheap answer was accepted without checking"


def test_a_good_cheap_answer_is_not_escalated():
    """This is where the saving comes from: the strong model never runs."""
    seen: list[str] = []

    async def call(model):
        seen.append(model)
        return "a perfectly good answer"

    _run(execute("keyword_extraction", call, validator=lambda v: True))
    assert seen == ["cheap-1"]


# --- caching and coalescing -------------------------------------------------

def test_a_cached_answer_is_not_requested_again():
    calls = {"n": 0}
    cache: dict = {}

    async def call(model):
        calls["n"] += 1
        return "answer"

    key = cache_key("keyword_extraction", "cheap-1", "prompt")
    for _ in range(3):
        _run(execute("keyword_extraction", call, cache=cache, key=key))
    assert calls["n"] == 1


def test_the_cache_key_changes_with_the_model():
    """Serving a cheap answer for a strong request would undo an escalation."""
    assert cache_key("t", "cheap-1", "p") != cache_key("t", "strong-1", "p")


def test_the_cache_key_changes_with_the_prompt_and_system():
    assert cache_key("t", "m", "a") != cache_key("t", "m", "b")
    assert cache_key("t", "m", "p", "sys-a") != cache_key("t", "m", "p", "sys-b")


def test_caching_off_means_every_call_runs(monkeypatch):
    monkeypatch.setenv("AI_CACHE_ENABLED", "false")
    calls = {"n": 0}
    cache: dict = {}

    async def call(model):
        calls["n"] += 1
        return "answer"

    for _ in range(3):
        _run(execute("keyword_extraction", call, cache=cache, key="k"))
    assert calls["n"] == 3


def test_ten_identical_requests_make_one_provider_call():
    calls = {"n": 0}

    async def main():
        flight = SingleFlight()

        async def call():
            calls["n"] += 1
            await asyncio.sleep(0.02)
            return "answer"

        results = await asyncio.gather(
            *[flight.run("same-key", call) for _ in range(10)])
        return results, flight

    results, flight = _run(main())
    assert results == ["answer"] * 10
    assert calls["n"] == 1
    assert flight.coalesced == 9


def test_a_coalesced_failure_reaches_every_waiter():
    async def main():
        flight = SingleFlight()

        async def call():
            await asyncio.sleep(0.01)
            raise RuntimeError("provider down")

        return await asyncio.gather(
            *[flight.run("k", call) for _ in range(3)],
            return_exceptions=True,
        )

    outcomes = _run(main())
    assert all(isinstance(o, RuntimeError) for o in outcomes)


def test_the_key_clears_after_a_failure_so_a_retry_is_possible():
    calls = {"n": 0}

    async def main():
        flight = SingleFlight()

        async def failing():
            calls["n"] += 1
            raise RuntimeError("down")

        for _ in range(2):
            try:
                await flight.run("k", failing)
            except RuntimeError:
                pass
        return calls["n"]

    assert _run(main()) == 2


# --- budget -----------------------------------------------------------------

def test_a_budget_stops_runaway_calls():
    budget = Budget(max_calls=2)

    async def call(model):
        return "ok"

    for _ in range(2):
        _run(execute("keyword_extraction", call, budget=budget))
    assert budget.calls == 2
    assert route("keyword_extraction", requested="caller", budget=budget) == "caller"
    assert budget.denied == 1


def test_expensive_models_have_their_own_ceiling():
    budget = Budget(max_expensive_calls=1)
    assert budget.allows(Tier.STRONG) is True
    budget.record(Tier.STRONG)
    assert budget.allows(Tier.STRONG) is False
    # A cheap model is still allowed once the expensive ceiling is reached.
    assert budget.allows(Tier.CHEAP) is True


def test_a_budgeted_route_prefers_a_cheaper_model_over_refusing():
    budget = Budget(max_expensive_calls=0)
    budget.max_expensive_calls = 1
    budget.record(Tier.STRONG)
    # fact_check needs strong; with strong exhausted the caller's model stands.
    assert route("fact_check", requested="caller", budget=budget) == "caller"


# --- OmniRoute --------------------------------------------------------------

def test_omniroute_is_off_unless_the_mode_says_so(monkeypatch):
    monkeypatch.setenv("AI_GATEWAY_MODE", "direct")
    monkeypatch.setenv("AI_GATEWAY_BASE_URL", "https://gw.example/v1")
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "k")
    assert omniroute_available() is False


def test_omniroute_without_credentials_falls_back_to_direct(monkeypatch):
    """A half-configured gateway must not take the pipeline down."""
    monkeypatch.setenv("AI_GATEWAY_MODE", "omniroute")
    monkeypatch.delenv("AI_GATEWAY_BASE_URL", raising=False)
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(gw, "_setting", lambda name, default="": {
        "AI_GATEWAY_MODE": "omniroute"}.get(name, default))
    assert omniroute_available() is False


def test_omniroute_is_used_when_fully_configured(monkeypatch):
    monkeypatch.setattr(gw, "_setting", lambda name, default="": {
        "AI_GATEWAY_MODE": "omniroute",
        "AI_GATEWAY_BASE_URL": "https://gw.example/v1",
        "AI_GATEWAY_API_KEY": "k",
    }.get(name, default))
    assert omniroute_available() is True
    assert gw.gateway_mode() is GatewayMode.OMNIROUTE


def test_a_completion_goes_through_the_openai_compatible_endpoint(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization", "")
        seen["body"] = _json.loads(request.content)
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "routed answer"}}]})

    monkeypatch.setattr(gw, "_setting", lambda name, default="": {
        "AI_GATEWAY_BASE_URL": "https://gw.example/v1",
        "AI_GATEWAY_API_KEY": "secret",
    }.get(name, default))

    text = _run(omniroute_generate(
        "the prompt", model="gemini-3-flash", system_instruction="be brief",
        response_json=True,
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))

    assert text == "routed answer"
    assert seen["url"] == "https://gw.example/v1/chat/completions"
    assert seen["auth"] == "Bearer secret"
    assert seen["body"]["model"] == "gemini-3-flash"
    assert seen["body"]["response_format"] == {"type": "json_object"}
    assert seen["body"]["messages"][0]["role"] == "system"
    assert "secret" not in seen["url"]


def test_an_empty_completion_is_an_error_not_an_empty_answer(monkeypatch):
    monkeypatch.setattr(gw, "_setting", lambda name, default="": {
        "AI_GATEWAY_BASE_URL": "https://gw.example/v1",
        "AI_GATEWAY_API_KEY": "k",
    }.get(name, default))

    with pytest.raises(RuntimeError, match="no choices"):
        _run(omniroute_generate(
            "p", model="m",
            client=httpx.AsyncClient(transport=httpx.MockTransport(
                lambda r: httpx.Response(200, json={"choices": []})))))


def test_an_unconfigured_gateway_refuses_rather_than_guessing_a_url(monkeypatch):
    monkeypatch.setattr(gw, "_setting", lambda name, default="": default)
    with pytest.raises(RuntimeError, match="not configured"):
        _run(omniroute_generate("p", model="m"))


# --- observability ----------------------------------------------------------

def test_the_run_reports_what_it_did():
    async def call(model):
        if model == "cheap-1":
            raise RuntimeError("429")
        return "ok"

    _run(execute("keyword_extraction", call))
    report = metrics()

    assert report["requests"] >= 2
    assert report["failed"] == 1
    assert report["fallbacks"] >= 1
    assert "by_model" in report and "health" in report


def test_cache_hits_are_counted():
    cache: dict = {}

    async def call(model):
        return "answer"

    key = cache_key("keyword_extraction", "cheap-1", "p")
    _run(execute("keyword_extraction", call, cache=cache, key=key))
    _run(execute("keyword_extraction", call, cache=cache, key=key))

    assert metrics()["cache_hits"] == 1


def test_the_mode_and_flags_are_reported():
    report = metrics()
    assert report["mode"] == "direct"
    assert report["routing_enabled"] is True


# --- defaults preserve current behaviour ------------------------------------

def test_every_flag_defaults_to_the_current_behaviour(monkeypatch):
    for name in (
        "AI_GATEWAY_MODE", "AI_SMART_ROUTING_ENABLED",
        "AI_CACHE_ENABLED", "AI_REQUEST_COALESCING_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(gw, "_setting", lambda name, default="": default)
    monkeypatch.setattr(gw, "_flag", lambda name, default=False: default)

    assert gw.gateway_mode() is GatewayMode.DIRECT
    assert gw.routing_enabled() is False


def test_the_task_table_names_no_model():
    """Model names belong in the catalogue, not scattered through policy."""
    from core.ai_gateway import TASK_POLICY

    for tier, caps in TASK_POLICY.values():
        assert isinstance(tier, Tier)
        assert all(isinstance(c, Capability) for c in caps)


def test_gemini_tts_is_not_routed():
    """Narration stays exactly as it is; no task maps to speech."""
    from core.ai_gateway import TASK_POLICY

    assert not any(
        Capability.AUDIO in caps for _, caps in TASK_POLICY.values())
