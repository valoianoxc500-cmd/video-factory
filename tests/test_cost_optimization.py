"""Cost accounting, budget enforcement, caching and the spend ceilings.

Measured on a real run before any of this existed: one video's model spend was
$1.27, of which `open_library_candidate_selection` alone was 37% -- 92 vision
calls each shipping an eight-image shortlist. The cost is almost entirely
*input* tokens, and input scales with the number of images, so the levers here
are the size of the shortlist and the number of times it is ranked.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import image_sourcer
from core.cost_guard import (
    DEFAULT_BUDGET_USD,
    RunCost,
    price_call,
    summarise_run,
)
from core.utils import ImageSourcingConfig, load_channel_config

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def rates() -> dict:
    data = json.loads(
        (ROOT / "config" / "pricing" / "google_ai_pricing.json").read_text("utf-8"))
    return {(e["service"], e["model"]): e for e in data["entries"]}


# --- pricing coverage -------------------------------------------------------

DEPLOYED = [
    ("generate_content", "gemini-2.5-pro"),
    ("generate_content", "gemini-3-flash-preview"),
    ("generate_content_image", "gemini-3.1-flash-lite-image"),
    ("generate_content_image", "gemini-2.5-flash-image"),
    ("tts", "gemini-2.5-pro-tts"),
]


@pytest.mark.parametrize("service,model", DEPLOYED)
def test_every_deployed_model_is_priced(service, model, rates):
    """An unpriced model is unknown spend, which makes a budget meaningless."""
    assert (service, model) in rates, f"{service}/{model} has no pricing entry"


@pytest.mark.parametrize("service,model", DEPLOYED)
def test_every_price_records_its_source(service, model, rates):
    entry = rates[(service, model)]
    assert entry.get("source_url"), f"{model} price has no source"


def test_the_settings_models_are_all_priced(rates):
    """Catches a model rename that would silently stop being costed."""
    from settings import settings

    for attr, service in (
        ("gemini_primary_model", "generate_content"),
        ("gemini_review_model", "generate_content"),
        ("gemini_research_model", "generate_content"),
    ):
        model = getattr(settings, attr, "")
        if model:
            assert (service, model) in rates, f"{attr}={model} is unpriced"


# --- pricing maths ----------------------------------------------------------

def test_token_pricing_uses_the_catalogue_rates(rates):
    usd = price_call(
        rates, service="generate_content", model="gemini-3-flash-preview",
        tokens_in=1_000_000, tokens_out=0)
    assert usd == pytest.approx(0.5)


def test_image_output_is_billed_per_image_not_per_token(rates):
    usd = price_call(
        rates, service="generate_content_image",
        model="gemini-3.1-flash-lite-image", tokens_in=0, images=1)
    assert usd == pytest.approx(0.0336)


def test_an_unpriced_model_returns_none_not_zero(rates):
    """Treating unknown spend as free is how a budget stops meaning anything."""
    assert price_call(rates, service="generate_content",
                      model="some-unreleased-model", tokens_in=1000) is None


# --- accounting -------------------------------------------------------------

def test_unpriced_calls_are_counted_separately():
    cost = RunCost(budget_usd=0.10)
    cost.record(operation="x", model="known", usd=0.02)
    cost.record(operation="y", model="mystery", usd=None)

    assert cost.priced_usd == pytest.approx(0.02)
    assert cost.unpriced_calls == 1
    assert "mystery" in cost.unpriced_models
    assert cost.requests == 2


def test_the_record_answers_what_this_video_cost():
    cost = RunCost(budget_usd=0.10)
    cost.record(operation="image_review", model="flash", usd=0.03)
    cost.cache_hits = 4
    cost.serper_searches = 3
    cost.images_generated = 2

    record = cost.to_record()
    for key in (
        "estimated_cost_usd", "budget_usd", "budget_used_pct", "over_budget",
        "unpriced_calls", "total_requests", "requests_by_model",
        "cost_by_operation", "cache_hits", "retries", "images_generated",
        "serper_searches", "tts_calls",
    ):
        assert key in record


def test_costs_are_attributed_to_operations():
    cost = RunCost()
    cost.record(operation="candidate_selection", model="flash", usd=0.04)
    cost.record(operation="candidate_selection", model="flash", usd=0.02)
    cost.record(operation="final_review", model="flash", usd=0.01)

    assert cost.to_record()["cost_by_operation"]["candidate_selection"] == pytest.approx(0.06)


# --- budget enforcement -----------------------------------------------------

def test_the_default_budget_is_ten_cents():
    assert DEFAULT_BUDGET_USD == 0.10
    assert ImageSourcingConfig().cost_budget_usd == 0.10


def test_the_budget_is_configurable_per_channel():
    config = load_channel_config("horror_stories")
    assert hasattr(config.image_sourcing, "cost_budget_usd")


def test_optional_work_is_declined_once_the_budget_is_spent():
    cost = RunCost(budget_usd=0.10)
    cost.record(operation="x", model="m", usd=0.10)

    assert cost.over_budget is True
    assert cost.allows_optional(0.01) is False


def test_optional_work_is_allowed_while_there_is_room():
    cost = RunCost(budget_usd=0.10)
    cost.record(operation="x", model="m", usd=0.02)

    assert cost.allows_optional(0.01) is True
    assert cost.over_budget is False


def test_a_call_that_would_exceed_the_budget_is_declined():
    cost = RunCost(budget_usd=0.10)
    cost.record(operation="x", model="m", usd=0.095)
    assert cost.allows_optional(0.02) is False


def test_the_run_tightens_before_it_runs_out():
    """Optional spend stops short of the ceiling, leaving room for required
    work like the review gates."""
    cost = RunCost(budget_usd=0.10)
    cost.record(operation="x", model="m", usd=0.08)
    assert cost.should_tighten is True
    assert cost.over_budget is False


# --- the spend ceilings in sourcing ----------------------------------------

def test_the_open_library_shortlist_is_smaller_than_the_pexels_one():
    """Its candidates carry no relevance ordering, so a deeper list costs
    input tokens without improving the pick."""
    assert image_sourcer._OPEN_LIBRARY_CANDIDATE_COUNT < \
        image_sourcer._PEXELS_CANDIDATE_COUNT
    assert image_sourcer._OPEN_LIBRARY_CANDIDATE_COUNT == 4


def test_a_run_has_a_ceiling_on_vision_selection_calls():
    assert image_sourcer._CANDIDATE_SELECTION_BUDGET > 0


def test_the_selection_ceiling_resets_between_runs():
    image_sourcer._CANDIDATE_SELECTION_CALLS = 99
    image_sourcer._reset_provenance()
    assert image_sourcer._CANDIDATE_SELECTION_CALLS == 0


def test_past_the_ceiling_a_ranked_list_keeps_its_top_result(monkeypatch, tmp_path):
    """No model call, and no beat lost: the Pexels list really is ranked."""
    import asyncio

    def unreachable(**kwargs):  # pragma: no cover - must not run
        raise AssertionError("called the vision model past the ceiling")

    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", unreachable)
    monkeypatch.setattr(image_sourcer, "_CANDIDATE_SELECTION_CALLS",
                        image_sourcer._CANDIDATE_SELECTION_BUDGET)

    first = tmp_path / "a.jpg"
    first.write_bytes(b"x")
    winner = asyncio.run(image_sourcer._select_photo_candidate(
        source_name="Pexels", keywords="k", prompt="",
        candidate_paths=[first, tmp_path / "b.jpg"],
        operation_label="pexels_candidate_selection",
    ))
    assert winner == first
    image_sourcer._reset_selection_budget()


def test_past_the_ceiling_an_unranked_list_drops_the_beat(monkeypatch, tmp_path):
    """The open-library list is not relevance-ranked, so its top result is a
    coin toss -- dropping is correct where keeping would be for Pexels."""
    import asyncio

    monkeypatch.setattr(
        image_sourcer.clients, "review_with_vision",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("called")))
    monkeypatch.setattr(image_sourcer, "_CANDIDATE_SELECTION_CALLS",
                        image_sourcer._CANDIDATE_SELECTION_BUDGET)

    first = tmp_path / "a.jpg"
    first.write_bytes(b"x")
    winner = asyncio.run(image_sourcer._select_photo_candidate(
        source_name="open libraries", keywords="k", prompt="",
        candidate_paths=[first],
        operation_label="open_library_candidate_selection",
        fallback_to_top=False,
    ))
    assert winner is None
    image_sourcer._reset_selection_budget()


# --- the answer cache -------------------------------------------------------

def test_a_deterministic_answer_is_cached_by_everything_that_changes_it():
    from core.ai_gateway import cache_key

    base = cache_key("op", "model-a", "prompt", "system", "extra")
    assert base != cache_key("op", "model-b", "prompt", "system", "extra")
    assert base != cache_key("op", "model-a", "other", "system", "extra")
    assert base != cache_key("op", "model-a", "prompt", "other", "extra")
    assert base != cache_key("op", "model-a", "prompt", "system", "other")
    assert base == cache_key("op", "model-a", "prompt", "system", "extra")


def test_the_client_cache_can_be_cleared_between_runs():
    import clients

    clients._GATEWAY_CACHE["k"] = "v"
    clients.reset_gateway_cache()
    assert clients._GATEWAY_CACHE == {}


def test_only_deterministic_calls_are_cached():
    """At a high temperature the caller is asking for variety, and serving a
    cached answer would remove it."""
    import inspect

    import clients

    source = inspect.getsource(clients._generate_text_response)
    assert "temperature <= 0.4" in source


# --- the summary a finished run writes --------------------------------------

def test_a_finished_run_is_priced_from_its_own_trace(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "ai_trace_report.json").write_text(json.dumps({"traces": [
        {"service": "generate_content", "model": "gemini-3-flash-preview",
         "operation": "image_review", "status": "ok",
         "response": {"prompt_token_count": 1_000_000, "output_token_count": 0}},
        {"service": "generate_content_image",
         "model": "gemini-3.1-flash-lite-image",
         "operation": "generated_fallback", "status": "ok", "response": {}},
    ]}), encoding="utf-8")

    record = summarise_run(tmp_path, ROOT, budget_usd=0.10)

    assert record["total_requests"] == 2
    assert record["images_generated"] == 1
    assert record["estimated_cost_usd"] == pytest.approx(0.5 + 0.0336, abs=1e-4)
    assert record["over_budget"] is True
    assert (tmp_path / "reports" / "run_cost.json").exists()


def test_a_failed_run_still_accounts_for_what_it_spent(tmp_path):
    reports = tmp_path / "reports"
    reports.mkdir(parents=True)
    (reports / "ai_trace_report.json").write_text(json.dumps({"traces": [
        {"service": "generate_content", "model": "gemini-3-flash-preview",
         "operation": "script_generate", "status": "error",
         "response": {"prompt_token_count": 10_000, "output_token_count": 100}},
    ]}), encoding="utf-8")

    record = summarise_run(tmp_path, ROOT)
    assert record["total_requests"] == 1
    assert record["estimated_cost_usd"] > 0


def test_no_trace_means_no_summary_rather_than_a_wrong_one(tmp_path):
    assert summarise_run(tmp_path, ROOT) == {}
