"""What one generation actually costs us.

Almost nothing, by design. Narration is Edge TTS (free, keyless), footage is
Pexels and Pixabay (free tiers), captions and composition are local FFmpeg and
libass. The only metered thing in the whole pipeline is the single script call.

Recorded rather than estimated from a price list, so the number stored against
a finished video is the tokens that call actually used. It is written to the
job row so the real average can be measured later instead of guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["CostLedger", "MODEL_RATES"]

#: USD per 1M tokens, (input, output). Only the models this product can
#: actually reach. An unlisted model falls back to the cheapest known rate and
#: is flagged, so a model swap shows up as an unpriced line rather than as a
#: silent zero.
MODEL_RATES: dict[str, tuple[float, float]] = {
    "gemini-3.1-pro-preview": (1.25, 10.00),
    "gemini-3-flash-preview": (0.30, 2.50),
    "gemini-2.5-flash": (0.30, 2.50),
}
_FALLBACK_RATE = (0.30, 2.50)


@dataclass
class CostLedger:
    """Per-run spend, itemised."""

    items: list[dict] = field(default_factory=list)
    unpriced_models: list[str] = field(default_factory=list)

    def record_model_call(
        self, *, model: str, input_tokens: int, output_tokens: int, label: str
    ) -> float:
        rate_in, rate_out = MODEL_RATES.get(model, _FALLBACK_RATE)
        if model not in MODEL_RATES and model not in self.unpriced_models:
            self.unpriced_models.append(model)
        usd = (input_tokens * rate_in + output_tokens * rate_out) / 1_000_000
        self.items.append({
            "label": label,
            "model": model,
            "input_tokens": int(input_tokens),
            "output_tokens": int(output_tokens),
            "usd": round(usd, 6),
        })
        return usd

    def record_free(self, label: str, detail: str = "") -> None:
        """A stage that cost nothing, recorded so the itemisation is complete.

        Worth writing down: "voice: $0.00 (edge)" is the line that shows the
        narration really is free, rather than merely missing from the total.
        """
        self.items.append({"label": label, "usd": 0.0, "detail": detail})

    @property
    def total_usd(self) -> float:
        return round(sum(item.get("usd", 0.0) for item in self.items), 6)

    def summary(self) -> dict:
        return {
            "total_usd": self.total_usd,
            "items": list(self.items),
            "unpriced_models": list(self.unpriced_models),
        }
