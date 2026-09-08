"""Which model runs a task, where it runs, and what to do when it fails.

Why this is a policy module and not a client
--------------------------------------------
`clients.py` is already the single place every Gemini call goes through, and it
already has backoff on 429s, a record/replay cache, per-call tracing and cost
labelling. Building a second AI client beside it would duplicate all of that
and leave two things to keep in step. So this module holds only the decisions
`clients.py` cannot make for itself:

    which model should run this task, and what should happen when it fails

`clients.py` asks; this answers. Nothing here opens a socket to a model
provider except the OmniRoute path, which is a different transport for the same
question.

Everything is off by default
----------------------------
`AI_GATEWAY_MODE` defaults to `direct` and `AI_SMART_ROUTING_ENABLED` to false,
so with no configuration the pipeline behaves exactly as it did: the model each
call site already asks for, the retry ladder already in `clients.py`, and no
new failure modes. Each capability is a separate flag so it can be rolled back
on its own.

What "cheaper" means here
-------------------------
Not "always the small model". A task declares the quality it needs, and the
policy picks the cheapest model that meets it. Escalation is available when a
cheap answer fails validation -- which is how this saves money without costing
quality: the expensive model runs on the few cases that need it rather than on
everything.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger("video_factory")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        try:
            from settings import settings

            value = getattr(settings, name.lower(), None)
            if value is not None:
                return bool(value)
        except Exception:
            pass
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _setting(name: str, default: str = "") -> str:
    raw = os.environ.get(name)
    if raw:
        return raw.strip()
    try:
        from settings import settings

        return str(getattr(settings, name.lower(), "") or "").strip() or default
    except Exception:
        return default


class GatewayMode(str, Enum):
    DIRECT = "direct"
    OMNIROUTE = "omniroute"


def gateway_mode() -> GatewayMode:
    raw = _setting("AI_GATEWAY_MODE", "direct").lower()
    try:
        return GatewayMode(raw)
    except ValueError:
        logger.warning(f"unknown AI_GATEWAY_MODE {raw!r}; using direct")
        return GatewayMode.DIRECT


def routing_enabled() -> bool:
    return _flag("AI_SMART_ROUTING_ENABLED", False)


def cache_enabled() -> bool:
    return _flag("AI_CACHE_ENABLED", True)


def coalescing_enabled() -> bool:
    return _flag("AI_REQUEST_COALESCING_ENABLED", True)


# ---------------------------------------------------------------------------
# Capabilities and tiers
# ---------------------------------------------------------------------------

class Capability(str, Enum):
    TEXT = "text"
    JSON = "json"
    VISION = "vision"
    SEARCH = "search"          # web-grounded research
    IMAGE = "image"            # image generation
    AUDIO = "audio"            # speech synthesis


class Tier(str, Enum):
    """How much thinking a task actually needs."""

    CHEAP = "cheap"
    BALANCED = "balanced"
    STRONG = "strong"
    VERY_STRONG = "very_strong"


_TIER_ORDER = [Tier.CHEAP, Tier.BALANCED, Tier.STRONG, Tier.VERY_STRONG]


@dataclass(frozen=True)
class ModelSpec:
    """One model, and what it can actually be asked to do."""

    name: str
    tier: Tier
    capabilities: frozenset[Capability]
    #: Relative cost, for choosing between models that both qualify. Not a
    #: price: real prices live in config/pricing and are applied by core.costs.
    weight: float = 1.0

    def supports(self, required: frozenset[Capability]) -> bool:
        return required <= self.capabilities


def _default_catalogue() -> list[ModelSpec]:
    """The Gemini models this deployment already uses, described.

    Names come from settings so a model rename stays in one place. A model the
    settings do not name is simply absent from routing.
    """
    from settings import settings

    text = frozenset({Capability.TEXT, Capability.JSON})
    vision = text | {Capability.VISION}

    catalogue: list[ModelSpec] = []

    def add(name: str, tier: Tier, caps: frozenset[Capability], weight: float):
        if name and not any(m.name == name for m in catalogue):
            catalogue.append(ModelSpec(name, tier, caps, weight))

    # Tier is the *ceiling of work a model can carry*, not how cheap it is.
    # The flash model was declared CHEAP, which meant a task needing BALANCED
    # excluded it and fell through to the expensive model -- the opposite of
    # the intent. It is the workhorse here, so it is BALANCED, and a CHEAP
    # task still picks it because `candidates` accepts anything at or above
    # the floor and sorts by cost.
    add(settings.gemini_review_model, Tier.BALANCED, frozenset(vision), 0.2)
    add(settings.gemini_research_model, Tier.BALANCED,
        frozenset(vision | {Capability.SEARCH}), 0.3)
    add(settings.gemini_primary_model, Tier.STRONG, frozenset(vision), 1.0)
    add(settings.gemini_image_model, Tier.BALANCED,
        frozenset({Capability.IMAGE}), 0.5)
    return catalogue


#: task -> (tier it needs, capabilities it needs)
#
# Deliberately one table rather than model names scattered through the
# pipeline. A task appears here or it is not routed, and an unrouted task keeps
# whatever model its call site already passed.
TASK_POLICY: dict[str, tuple[Tier, frozenset[Capability]]] = {
    # Cheap: mechanical transformations of text that is already correct.
    "keyword_extraction": (Tier.CHEAP, frozenset({Capability.JSON})),
    "query_generation": (Tier.CHEAP, frozenset({Capability.JSON})),
    "scene_tagging": (Tier.CHEAP, frozenset({Capability.JSON})),
    "topic_classification": (Tier.CHEAP, frozenset({Capability.JSON})),
    "metadata_extraction": (Tier.CHEAP, frozenset({Capability.JSON})),
    "scene_audio_plan": (Tier.CHEAP, frozenset({Capability.JSON})),

    # Balanced: writing and judgement where a mistake is visible but not fatal.
    "script": (Tier.BALANCED, frozenset({Capability.JSON})),
    # Checking and repairing a script is not authoring one. `script_generate`
    # deliberately stays unrouted so it keeps the quality model; these two were
    # 27% of a measured run's spend between them, and they are the halves that
    # read and correct text rather than invent it.
    "script_review": (Tier.BALANCED, frozenset({Capability.JSON})),
    "script_validation_revision": (Tier.BALANCED, frozenset({Capability.JSON})),
    "scene_planning": (Tier.BALANCED, frozenset({Capability.JSON})),
    "research_synthesis": (Tier.BALANCED,
                           frozenset({Capability.JSON, Capability.SEARCH})),
    "image_relevance": (Tier.BALANCED,
                        frozenset({Capability.JSON, Capability.VISION})),
    # Picking the best of a handful of candidate photographs is a comparison,
    # not a judgement call, and it runs once per beat -- so on the primary
    # model it was the single biggest consumer of quota in a run, and the one
    # that exhausted it. The cheap vision model does this well.
    "pexels_candidate_selection": (
        Tier.CHEAP, frozenset({Capability.JSON, Capability.VISION})),
    "open_library_candidate_selection": (
        Tier.CHEAP, frozenset({Capability.JSON, Capability.VISION})),
    "viral_analysis": (Tier.BALANCED, frozenset({Capability.JSON})),

    # Strong: where being wrong ships a wrong video.
    "fact_check": (Tier.STRONG, frozenset({Capability.JSON})),
    # Reading a finished video against a checklist is judgement the flash
    # model does well, and it already ran there by default -- routing it to
    # the strong model would have *raised* the bill for no measured gain.
    # Escalation on a rejected verdict is still available through `execute`.
    "final_review": (Tier.BALANCED,
                     frozenset({Capability.JSON, Capability.VISION})),
    "recreate_analysis": (Tier.STRONG, frozenset({Capability.JSON})),
}


class ModelPolicy:
    """Chooses a model for a task, and the order to fall back through."""

    def __init__(self, catalogue: list[ModelSpec] | None = None) -> None:
        self._catalogue = catalogue if catalogue is not None else _default_catalogue()

    @property
    def catalogue(self) -> list[ModelSpec]:
        return list(self._catalogue)

    def candidates(self, task: str) -> list[ModelSpec]:
        """Every model that can do this task, cheapest acceptable first.

        A model below the required tier is excluded rather than tried and
        allowed to fail: routing a fact check to a model that cannot do the
        reasoning is not a saving, it is a wrong answer at a discount.
        """
        policy = TASK_POLICY.get(task)
        if policy is None:
            return []
        tier, required = policy
        floor = _TIER_ORDER.index(tier)

        eligible = [
            model for model in self._catalogue
            if model.supports(required)
            and _TIER_ORDER.index(model.tier) >= floor
        ]
        # Cheapest that still clears the bar, then upward as fallbacks.
        return sorted(
            eligible, key=lambda m: (_TIER_ORDER.index(m.tier), m.weight))

    def choose(self, task: str, *, default: str = "") -> str:
        """The model to try first, or `default` when the task is not routed."""
        candidates = self.candidates(task)
        return candidates[0].name if candidates else default

    def escalate(self, task: str, after: str) -> str:
        """The next model up after `after` failed or produced a poor answer."""
        candidates = self.candidates(task)
        for index, model in enumerate(candidates):
            if model.name == after and index + 1 < len(candidates):
                return candidates[index + 1].name
        return ""


# ---------------------------------------------------------------------------
# Provider health
# ---------------------------------------------------------------------------

class Health(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    COOLDOWN = "cooldown"


@dataclass
class _ModelHealth:
    failures: int = 0
    successes: int = 0
    cooldown_until: float = 0.0

    def state(self, now: float) -> Health:
        if now < self.cooldown_until:
            return Health.COOLDOWN
        if self.failures >= 2 and self.failures > self.successes:
            return Health.DEGRADED
        return Health.HEALTHY


class HealthTracker:
    """Rolling success/failure per model, with a cooldown after rate limits.

    The point is not to stop using a model forever. It is to stop sending the
    next twenty requests into a model that just said "slow down", when another
    model could answer them now.
    """

    #: How long a model sits out after a rate limit.
    COOLDOWN_SECONDS = 45.0

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._models: dict[str, _ModelHealth] = {}
        self._clock = clock

    def _entry(self, model: str) -> _ModelHealth:
        return self._models.setdefault(model, _ModelHealth())

    def record_success(self, model: str) -> None:
        entry = self._entry(model)
        entry.successes += 1
        entry.failures = 0
        entry.cooldown_until = 0.0

    def record_failure(self, model: str, *, rate_limited: bool = False) -> None:
        entry = self._entry(model)
        entry.failures += 1
        if rate_limited:
            entry.cooldown_until = self._clock() + self.COOLDOWN_SECONDS

    def state(self, model: str) -> Health:
        return self._entry(model).state(self._clock())

    def usable(self, model: str) -> bool:
        return self.state(model) is not Health.COOLDOWN

    def snapshot(self) -> dict[str, str]:
        return {name: entry.state(self._clock()).value
                for name, entry in self._models.items()}


HEALTH = HealthTracker()


# ---------------------------------------------------------------------------
# Single-flight and caching
# ---------------------------------------------------------------------------

def cache_key(task: str, model: str, prompt: str, system: str = "",
              extra: str = "") -> str:
    """A key that changes whenever the answer would.

    The model is in the key on purpose: the same prompt answered by a cheap
    model and by a strong one are different answers, and serving one for the
    other would silently undo an escalation.
    """
    digest = hashlib.sha256()
    for part in (task, model, system, prompt, extra):
        digest.update(str(part).encode("utf-8", "replace"))
        digest.update(b"\x00")
    return f"{task}:{digest.hexdigest()[:32]}"


class SingleFlight:
    """One in-flight call per key; everyone else waits for its result.

    Ten scenes asking the same question at the same moment should cost one
    request, not ten. Callers that arrive while a call is running await the
    same future rather than starting their own.
    """

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future] = {}
        self.coalesced = 0

    async def run(self, key: str, call: Callable[[], Awaitable[Any]]) -> Any:
        existing = self._inflight.get(key)
        if existing is not None:
            self.coalesced += 1
            logger.debug(f"[gateway] coalescing onto in-flight {key}")
            return await asyncio.shield(existing)

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[key] = future
        try:
            result = await call()
        except BaseException as exc:
            # Everyone waiting sees the same failure, then the key clears so
            # the next caller may legitimately retry.
            if not future.done():
                future.set_exception(exc)
            raise
        else:
            if not future.done():
                future.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)


SINGLE_FLIGHT = SingleFlight()


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------

@dataclass
class Budget:
    """A ceiling on what one job may spend, in calls rather than dollars.

    Calls because that is what this layer can count reliably at decision time;
    real spend is reconciled afterwards by `core.costs` from the provider's own
    usage numbers.
    """

    max_calls: int = 0                 # 0 = unlimited
    max_expensive_calls: int = 0       # strong / very strong tiers
    calls: int = 0
    expensive_calls: int = 0
    denied: int = 0

    def allows(self, tier: Tier) -> bool:
        if self.max_calls and self.calls >= self.max_calls:
            return False
        if (
            self.max_expensive_calls
            and tier in {Tier.STRONG, Tier.VERY_STRONG}
            and self.expensive_calls >= self.max_expensive_calls
        ):
            return False
        return True

    def record(self, tier: Tier) -> None:
        self.calls += 1
        if tier in {Tier.STRONG, Tier.VERY_STRONG}:
            self.expensive_calls += 1

    def to_record(self) -> dict:
        return {
            "calls": self.calls,
            "expensive_calls": self.expensive_calls,
            "denied": self.denied,
            "max_calls": self.max_calls,
            "max_expensive_calls": self.max_expensive_calls,
        }


# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

@dataclass
class Attempt:
    task: str
    model: str
    status: str                 # ok | failed | cached | coalesced | denied
    latency_ms: float = 0.0
    error: str = ""
    fallback: bool = False
    escalated: bool = False


@dataclass
class GatewayLog:
    """What the gateway did, for the analytics the pipeline already writes."""

    attempts: list[Attempt] = field(default_factory=list)

    def add(self, attempt: Attempt) -> None:
        self.attempts.append(attempt)

    def metrics(self) -> dict:
        total = len(self.attempts)
        if not total:
            return {"requests": 0}
        ok = sum(1 for a in self.attempts if a.status == "ok")
        cached = sum(1 for a in self.attempts if a.status == "cached")
        failed = sum(1 for a in self.attempts if a.status == "failed")
        fallbacks = sum(1 for a in self.attempts if a.fallback)
        latencies = [a.latency_ms for a in self.attempts if a.latency_ms]
        by_model: dict[str, int] = {}
        for attempt in self.attempts:
            by_model[attempt.model] = by_model.get(attempt.model, 0) + 1
        return {
            "requests": total,
            "ok": ok,
            "failed": failed,
            "cache_hits": cached,
            "cache_hit_rate": round(cached / total, 3),
            "fallbacks": fallbacks,
            "fallback_rate": round(fallbacks / total, 3),
            "avg_latency_ms": round(sum(latencies) / len(latencies), 1)
            if latencies else 0.0,
            "by_model": by_model,
            "health": HEALTH.snapshot(),
        }


LOG = GatewayLog()


def reset_state() -> None:
    """Clear per-run state. Called between runs and by tests."""
    global HEALTH, SINGLE_FLIGHT, LOG
    HEALTH = HealthTracker()
    SINGLE_FLIGHT = SingleFlight()
    LOG = GatewayLog()


# ---------------------------------------------------------------------------
# Choosing a model for a call
# ---------------------------------------------------------------------------

_POLICY: ModelPolicy | None = None


def policy() -> ModelPolicy:
    global _POLICY
    if _POLICY is None:
        _POLICY = ModelPolicy()
    return _POLICY


def set_policy(new_policy: ModelPolicy | None) -> None:
    """Override the policy, for tests and for a deployment-specific catalogue."""
    global _POLICY
    _POLICY = new_policy


def route(
    task: str,
    *,
    requested: str = "",
    budget: Budget | None = None,
) -> str:
    """The model to use, or the requested one when routing is off.

    Falls back to `requested` in every uncertain case -- routing disabled, an
    unknown task, no healthy candidate, or a budget refusal -- so turning the
    flag on can change which model runs but never whether the call happens.
    """
    if not routing_enabled() or not task:
        return requested

    candidates = policy().candidates(task)
    if not candidates:
        return requested

    for model in candidates:
        if not HEALTH.usable(model.name):
            continue
        if budget is not None and not budget.allows(model.tier):
            continue
        return model.name

    # Everything is in cooldown or over budget. The caller's own model and the
    # retry ladder in clients.py are still better than refusing outright.
    if budget is not None:
        budget.denied += 1
    return requested


def is_rate_limited(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}"
    return any(
        marker in text
        for marker in ("429", "RESOURCE_EXHAUSTED", "rate limit", "Rate limit")
    )


async def execute(
    task: str,
    call: Callable[[str], Awaitable[Any]],
    *,
    requested: str = "",
    budget: Budget | None = None,
    validator: Callable[[Any], bool] | None = None,
    cache: dict | None = None,
    key: str = "",
) -> Any:
    """Run one AI call under the gateway's policy.

    `call(model)` performs the actual request. The gateway decides which model
    to hand it, retries on another model when one fails, escalates when the
    answer does not validate, and records what happened.

    Deliberately thin: the transport, the provider retry ladder and the trace
    all stay in `clients.py`. This adds the decisions around them.
    """
    chosen = route(task, requested=requested, budget=budget)

    if cache is not None and cache_enabled() and key and key in cache:
        LOG.add(Attempt(task, chosen, "cached"))
        return cache[key]

    async def attempt_chain() -> Any:
        model = chosen
        tried: list[str] = []
        last_error: BaseException | None = None

        while model and model not in tried:
            tried.append(model)
            spec = next(
                (m for m in policy().catalogue if m.name == model), None)
            tier = spec.tier if spec else Tier.BALANCED

            if budget is not None and not budget.allows(tier):
                budget.denied += 1
                LOG.add(Attempt(task, model, "denied"))
                break

            started = time.perf_counter()
            try:
                result = await call(model)
            except BaseException as exc:      # noqa: BLE001 - re-raised below
                last_error = exc
                rate_limited = is_rate_limited(exc)
                HEALTH.record_failure(model, rate_limited=rate_limited)
                LOG.add(Attempt(
                    task, model, "failed",
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=str(exc)[:200],
                    fallback=bool(tried[:-1]),
                ))
                nxt = policy().escalate(task, model) if routing_enabled() else ""
                if not nxt:
                    raise
                logger.warning(
                    f"[gateway] {task}: {model} failed "
                    f"({str(exc)[:90]}); falling back to {nxt}"
                )
                model = nxt
                continue

            HEALTH.record_success(model)
            if budget is not None:
                budget.record(tier)
            LOG.add(Attempt(
                task, model, "ok",
                latency_ms=(time.perf_counter() - started) * 1000,
                fallback=bool(tried[:-1]),
            ))

            # A cheap answer that does not hold up is escalated once rather
            # than accepted. This is what keeps cost routing from costing
            # quality.
            if validator is not None and not validator(result):
                nxt = policy().escalate(task, model) if routing_enabled() else ""
                if nxt and nxt not in tried:
                    logger.info(
                        f"[gateway] {task}: {model} answer rejected by "
                        f"validator; escalating to {nxt}"
                    )
                    LOG.attempts[-1].escalated = True
                    model = nxt
                    continue

            if cache is not None and cache_enabled() and key:
                cache[key] = result
            return result

        if last_error is not None:
            raise last_error
        raise RuntimeError(f"no model available for task {task!r}")

    if coalescing_enabled() and key:
        return await SINGLE_FLIGHT.run(key, attempt_chain)
    return await attempt_chain()


# ---------------------------------------------------------------------------
# OmniRoute transport
# ---------------------------------------------------------------------------

def omniroute_config() -> tuple[str, str]:
    return _setting("AI_GATEWAY_BASE_URL"), _setting("AI_GATEWAY_API_KEY")


def omniroute_available() -> bool:
    """Whether OmniRoute is configured well enough to be tried at all."""
    if gateway_mode() is not GatewayMode.OMNIROUTE:
        return False
    base, key = omniroute_config()
    if not base or not key:
        logger.warning(
            "AI_GATEWAY_MODE=omniroute but AI_GATEWAY_BASE_URL or "
            "AI_GATEWAY_API_KEY is unset; using the direct provider instead"
        )
        return False
    return True


async def omniroute_generate(
    prompt: str,
    *,
    model: str,
    system_instruction: str = "",
    response_json: bool = False,
    temperature: float = 1.0,
    max_output_tokens: int = 8192,
    client=None,
) -> str:
    """One completion through OmniRoute's OpenAI-compatible endpoint.

    Returns the assistant's text. Anything else -- image generation, speech,
    web-grounded research -- stays on the direct provider path, because those
    are not chat completions and routing them here would change their output.
    """
    import httpx

    base, api_key = omniroute_config()
    if not base or not api_key:
        raise RuntimeError("OmniRoute is not configured")

    messages: list[dict] = []
    if system_instruction:
        messages.append({"role": "system", "content": system_instruction})
    messages.append({"role": "user", "content": prompt})

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_output_tokens,
    }
    if response_json:
        payload["response_format"] = {"type": "json_object"}

    owns = client is None
    http = client or httpx.AsyncClient(timeout=180.0, follow_redirects=True)
    try:
        response = await http.post(
            f"{base.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        response.raise_for_status()
        body = response.json() or {}
    finally:
        if owns:
            await http.aclose()

    choices = body.get("choices") or []
    if not choices:
        raise RuntimeError("OmniRoute returned no choices")
    return str((choices[0].get("message") or {}).get("content") or "")


def metrics() -> dict:
    """Everything the gateway knows about this run, for the analytics layer."""
    return {
        "mode": gateway_mode().value,
        "routing_enabled": routing_enabled(),
        "cache_enabled": cache_enabled(),
        "coalescing_enabled": coalescing_enabled(),
        "coalesced_requests": SINGLE_FLIGHT.coalesced,
        **LOG.metrics(),
    }
