"""Hard, persisted paid-generation budget for one Animated Stories run."""

from __future__ import annotations

import json
from pathlib import Path

from core.utils import ChannelConfig


_ACTIVE: "AnimatedGenerationBudget | None" = None


class AnimatedGenerationBudget:
    """Conservative reservation ledger shared by every paid animation stage.

    Reservations happen before provider calls. A timeout may still have cost
    money at the provider, so it remains reserved; this is how the hard cap is
    real rather than an optimistic after-the-fact report.
    """

    def __init__(self, workspace: Path, ceiling_usd: float, spent_usd: float = 0.0, entries=None):
        self.workspace = workspace
        self.ceiling_usd = round(max(0.0, float(ceiling_usd)), 6)
        self.spent_usd = round(max(0.0, float(spent_usd)), 6)
        self.entries = list(entries or [])

    @property
    def remaining_usd(self) -> float:
        return max(0.0, round(self.ceiling_usd - self.spent_usd, 6))

    @property
    def path(self) -> Path:
        return self.workspace / "animated_generation_budget.json"

    def reserve(self, *, kind: str, cost_usd: float, detail: str = "") -> bool:
        cost = round(max(0.0, float(cost_usd)), 6)
        if cost > self.remaining_usd + 1e-9:
            return False
        self.spent_usd = round(self.spent_usd + cost, 6)
        self.entries.append({"kind": kind, "cost_usd": cost, "detail": detail[:160]})
        self.save()
        return True

    def save(self) -> None:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "ceiling_usd": self.ceiling_usd,
            "spent_usd": self.spent_usd,
            "remaining_usd": self.remaining_usd,
            "entries": self.entries,
        }, ensure_ascii=False, indent=2), encoding="utf-8")


def activate(workspace: Path, config: ChannelConfig) -> AnimatedGenerationBudget | None:
    """Open this run's budget, or remain inert for every other product."""
    global _ACTIVE
    if str(getattr(config, "channel_id", "")) != "animated_stories":
        _ACTIVE = None
        return None

    ceiling = float(getattr(config.animation, "total_generation_budget_usd", 0.40))
    path = workspace / "animated_generation_budget.json"
    data = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, json.JSONDecodeError):
        data = {}
    _ACTIVE = AnimatedGenerationBudget(
        workspace, ceiling,
        spent_usd=data.get("spent_usd", 0.0), entries=data.get("entries", []),
    )
    _ACTIVE.save()
    return _ACTIVE


def current(config: ChannelConfig) -> AnimatedGenerationBudget | None:
    if str(getattr(config, "channel_id", "")) != "animated_stories":
        return None
    return _ACTIVE


def reserve(config: ChannelConfig, *, kind: str, cost_usd: float, detail: str = "") -> bool:
    budget = current(config)
    return budget is None or budget.reserve(kind=kind, cost_usd=cost_usd, detail=detail)
