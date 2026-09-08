"""A per-video budget, and the end-of-run answer to "what did this cost?".

What this is not
----------------
Not a second cost tracker. `core.costs` already reconciles real spend from the
provider's own usage numbers, and the AI trace already records every call. This
reads those, prices them against the configured catalogue, and decides whether
the run may keep making optional paid calls.

What "optional" means
---------------------
The guard never blocks work the video cannot be finished without. Narration,
the review gates and the assembly stages run regardless -- stopping them would
produce an invalid video, which is worse than an expensive one. What it stops
is the discretionary spend: another catalogue search, another shortlist for a
vision model to rank, another generated image where a licensed one would do.

So the budget shapes *how* a beat is filled, never *whether* the video is
validated.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("video_factory")

#: Target cost of one finished video, in USD. Configurable per channel.
DEFAULT_BUDGET_USD = 0.10

#: Below this share of the budget the run is unconstrained; above it, optional
#: paid work is declined in favour of cached, licensed or cheaper routes.
TIGHTEN_AT = 0.75


def _catalogue(root: Path) -> dict:
    path = root / "config" / "pricing" / "google_ai_pricing.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text("utf-8"))
    return {(e["service"], e["model"]): e for e in data.get("entries", [])}


def price_call(
    rates: dict,
    *,
    service: str,
    model: str,
    tokens_in: int = 0,
    tokens_out: int = 0,
    images: int = 0,
) -> float | None:
    """USD for one call, or None when the model is not in the catalogue.

    None is deliberately not zero: an unpriced model is unknown spend, and
    treating it as free is how a budget silently stops meaning anything.
    """
    entry = rates.get((service, model))
    if entry is None:
        return None

    total = tokens_in / 1e6 * (entry.get("input_rate_usd_per_1m_tokens") or 0.0)
    per_image = entry.get("output_image_rate_usd_per_image")
    if images and per_image:
        total += images * per_image
    else:
        rate = (
            entry.get("output_text_rate_usd_per_1m_tokens")
            or entry.get("output_rate_usd_per_1m_tokens")
            or entry.get("output_audio_rate_usd_per_1m_tokens")
            or 0.0
        )
        total += tokens_out / 1e6 * rate
    return total


@dataclass
class RunCost:
    """Everything spent so far, and everything that could not be priced."""

    budget_usd: float = DEFAULT_BUDGET_USD
    priced_usd: float = 0.0
    unpriced_calls: int = 0
    unpriced_models: set[str] = field(default_factory=set)

    requests: int = 0
    by_model: dict[str, int] = field(default_factory=dict)
    by_operation: dict[str, float] = field(default_factory=dict)

    cache_hits: int = 0
    cache_misses: int = 0
    retries: int = 0
    images_generated: int = 0
    images_reused: int = 0
    serper_searches: int = 0
    tts_calls: int = 0

    def record(
        self,
        *,
        operation: str,
        model: str,
        usd: float | None,
        images: int = 0,
    ) -> None:
        self.requests += 1
        self.by_model[model] = self.by_model.get(model, 0) + 1
        self.images_generated += images
        if usd is None:
            self.unpriced_calls += 1
            self.unpriced_models.add(model)
            return
        self.priced_usd += usd
        self.by_operation[operation] = self.by_operation.get(operation, 0.0) + usd

    @property
    def spent_share(self) -> float:
        return self.priced_usd / self.budget_usd if self.budget_usd else 0.0

    @property
    def over_budget(self) -> bool:
        return self.priced_usd >= self.budget_usd

    @property
    def should_tighten(self) -> bool:
        """Whether optional paid work should now be declined."""
        return self.spent_share >= TIGHTEN_AT

    def allows_optional(self, estimated_usd: float = 0.0) -> bool:
        """Whether one more discretionary paid call is affordable.

        Required work never consults this -- see the module docstring.
        """
        if self.over_budget:
            return False
        return (self.priced_usd + max(0.0, estimated_usd)) <= self.budget_usd

    def to_record(self) -> dict:
        return {
            "budget_usd": round(self.budget_usd, 4),
            "estimated_cost_usd": round(self.priced_usd, 4),
            "budget_used_pct": round(self.spent_share * 100, 1),
            "over_budget": self.over_budget,
            "unpriced_calls": self.unpriced_calls,
            "unpriced_models": sorted(self.unpriced_models),
            "total_requests": self.requests,
            "requests_by_model": dict(sorted(self.by_model.items())),
            "cost_by_operation": {
                k: round(v, 5)
                for k, v in sorted(
                    self.by_operation.items(), key=lambda kv: -kv[1])
            },
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "retries": self.retries,
            "images_generated": self.images_generated,
            "images_reused": self.images_reused,
            "serper_searches": self.serper_searches,
            "tts_calls": self.tts_calls,
        }


def summarise_run(workspace: Path, root: Path, *, budget_usd: float = DEFAULT_BUDGET_USD) -> dict:
    """Price a finished run from its own trace, and write the summary.

    Answers "how much did this exact video cost?" from the records the run
    already wrote, rather than from a second set of counters that could drift
    from them.
    """
    trace_path = workspace / "reports" / "ai_trace_report.json"
    if not trace_path.exists():
        return {}

    rates = _catalogue(root)
    traces = json.loads(trace_path.read_text("utf-8")).get("traces") or []
    cost = RunCost(budget_usd=budget_usd)

    for entry in traces:
        service = str(entry.get("service") or "")
        model = str(entry.get("model") or "")
        response = entry.get("response") or {}
        images = 1 if (
            service == "generate_content_image" and entry.get("status") == "ok"
        ) else 0
        if service == "tts":
            cost.tts_calls += 1
        cost.record(
            operation=str(entry.get("operation") or ""),
            model=model,
            usd=price_call(
                rates,
                service=service,
                model=model,
                tokens_in=int(response.get("prompt_token_count") or 0),
                tokens_out=int(response.get("output_token_count") or 0),
                images=images,
            ),
            images=images,
        )

    try:
        from core import ai_gateway

        metrics = ai_gateway.metrics()
        cost.cache_hits = int(metrics.get("cache_hits") or 0)
        cost.retries = int(metrics.get("failed") or 0)
    except Exception:
        pass

    record = cost.to_record()
    out = workspace / "reports" / "run_cost.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2), encoding="utf-8")

    logger.info(
        f"[cost] {record['total_requests']} AI request(s), "
        f"${record['estimated_cost_usd']:.4f} of a ${budget_usd:.2f} budget "
        f"({record['budget_used_pct']}%)"
        + (f", {record['unpriced_calls']} unpriced" if cost.unpriced_calls else "")
    )
    return record
