"""Unified Google AI client, fixture cache, and per-run AI tracing."""

from __future__ import annotations

import asyncio
import hashlib
import html
import itertools
import json
import logging
import re
import shutil
import subprocess
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from threading import Lock
from types import SimpleNamespace
from typing import Any

import httpx
from google import genai
from google.genai import types

from core import costs
from settings import settings

logger = logging.getLogger("video_factory")

# ---------------------------------------------------------------------------
# Fixture cache
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(".fixtures")


class CacheMode(Enum):
    OFF = "off"
    RECORD = "record"
    REPLAY = "replay"


_mode = CacheMode.OFF
_channel_slug: str = ""


def set_mode(mode: str, channel_slug: str = "") -> None:
    """Set cache mode globally. Called from factory.py CLI."""
    global _mode, _channel_slug
    _mode = CacheMode(mode)
    _channel_slug = channel_slug
    if _mode != CacheMode.OFF:
        logger.info(f"Fixture cache: {_mode.value} (channel={channel_slug})")


def get_mode() -> CacheMode:
    return _mode


def _cache_dir(category: str) -> Path:
    base = FIXTURES_DIR / _channel_slug if _channel_slug else FIXTURES_DIR
    return base / category


def _cache_key(*args: Any) -> str:
    """SHA-256 hash of serialized arguments."""
    raw = json.dumps(args, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _replay_service_and_model(fn_name: str, kwargs: dict[str, Any]) -> tuple[str, str] | None:
    explicit_model = kwargs.get("model")
    mapping = {
        "generate_text": ("generate_content", explicit_model or settings.gemini_primary_model),
        "generate_json": ("generate_content", explicit_model or settings.gemini_primary_model),
        "review_with_vision": ("generate_content", explicit_model or settings.gemini_review_model),
        "generate_image_gemini": ("generate_content_image", explicit_model or settings.gemini_image_model),
        "generate_speech": ("tts", explicit_model or settings.gemini_tts_model),
    }
    return mapping.get(fn_name)


def _record_replay_event(fn_name: str, kwargs: dict[str, Any], cache_kind: str) -> None:
    replay_info = _replay_service_and_model(fn_name, kwargs)
    if replay_info is None:
        return
    service, model = replay_info
    costs.record_cache_hit(
        provider="fixture_cache",
        service=service,
        model=model,
        operation=fn_name,
        cache_kind=cache_kind,
        notes=["Replay served from fixture cache."],
    )


# ── Decorators ────────────────────────────────────────────────────

def cached_text(fn):
    """Cache decorator for async functions returning str (text generation)."""
    async def wrapper(*args, **kwargs):
        if _mode == CacheMode.OFF:
            return await fn(*args, **kwargs)

        key = _cache_key(fn.__name__, args, kwargs)
        cache_path = _cache_dir("text") / f"{key}.json"

        if _mode == CacheMode.REPLAY:
            if not cache_path.exists():
                raise RuntimeError(f"Fixture cache miss: {fn.__name__} key={key}")
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            logger.debug(f"[fixture] replay {fn.__name__} from {cache_path.name}")
            _record_replay_event(fn.__name__, kwargs, "fixture_replay")
            return data["result"]

        # RECORD mode
        result = await fn(*args, **kwargs)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"fn": fn.__name__, "key": key, "result": result}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.debug(f"[fixture] recorded {fn.__name__} → {cache_path.name}")
        return result

    wrapper.__name__ = fn.__name__
    wrapper.__wrapped__ = fn
    return wrapper


def cached_json(fn):
    """Cache decorator for async functions returning dict/list (JSON generation)."""
    async def wrapper(*args, **kwargs):
        if _mode == CacheMode.OFF:
            return await fn(*args, **kwargs)

        key = _cache_key(fn.__name__, args, kwargs)
        cache_path = _cache_dir("text") / f"{key}.json"

        if _mode == CacheMode.REPLAY:
            if not cache_path.exists():
                raise RuntimeError(f"Fixture cache miss: {fn.__name__} key={key}")
            data = json.loads(cache_path.read_text(encoding="utf-8"))
            logger.debug(f"[fixture] replay {fn.__name__} from {cache_path.name}")
            _record_replay_event(fn.__name__, kwargs, "fixture_replay")
            return data["result"]

        result = await fn(*args, **kwargs)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"fn": fn.__name__, "key": key, "result": result}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.debug(f"[fixture] recorded {fn.__name__} → {cache_path.name}")
        return result

    wrapper.__name__ = fn.__name__
    wrapper.__wrapped__ = fn
    return wrapper


def cached_file(category: str):
    """Cache decorator for async functions that write to output_path and return Path|None."""
    def decorator(fn):
        async def wrapper(*args, **kwargs):
            if _mode == CacheMode.OFF:
                return await fn(*args, **kwargs)

            # Extract output_path from args or kwargs
            output_path = kwargs.get("output_path") or (args[1] if len(args) > 1 else None)
            if output_path is None:
                return await fn(*args, **kwargs)

            # Build cache key from all args except output_path
            cache_args = {k: v for k, v in kwargs.items() if k != "output_path"}
            # Include positional args except output_path
            pos_args = list(args)
            if len(pos_args) > 1:
                pos_args = [pos_args[0]] + pos_args[2:]  # skip output_path at index 1
            key = _cache_key(fn.__name__, pos_args, cache_args)

            suffix = Path(str(output_path)).suffix or f".{category}"
            cache_path = _cache_dir(category) / f"{key}{suffix}"

            if _mode == CacheMode.REPLAY:
                if not cache_path.exists():
                    raise RuntimeError(f"Fixture cache miss: {fn.__name__} key={key}")
                output = Path(str(output_path))
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cache_path, output)
                logger.debug(f"[fixture] replay {fn.__name__} → {output.name}")
                _record_replay_event(fn.__name__, kwargs, "fixture_replay")
                return output

            # RECORD mode
            result = await fn(*args, **kwargs)
            if result and Path(str(output_path)).exists():
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(output_path), cache_path)
                logger.debug(f"[fixture] recorded {fn.__name__} → {cache_path.name}")
            return result

        wrapper.__name__ = fn.__name__
        wrapper.__wrapped__ = fn
        return wrapper
    return decorator


def cached_http(fn):
    """Cache decorator for async HTTP search functions returning bool + writing to output_path."""
    async def wrapper(*args, **kwargs):
        if _mode == CacheMode.OFF:
            return await fn(*args, **kwargs)

        key = _cache_key(fn.__name__, args[:1], {k: v for k, v in kwargs.items() if k not in ("client", "seen_hashes")})
        meta_path = _cache_dir("http") / f"{key}.json"

        # Extract output_path (2nd positional arg for search functions)
        output_path = args[1] if len(args) > 1 else kwargs.get("output_path")

        if _mode == CacheMode.REPLAY:
            bin_path = _cache_dir("http_bin") / f"{key}{Path(str(output_path)).suffix}"
            if not meta_path.exists() or not bin_path.exists():
                raise RuntimeError(f"Fixture cache miss: {fn.__name__} key={key}")
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if output_path:
                Path(str(output_path)).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bin_path, str(output_path))
            logger.debug(f"[fixture] replay {fn.__name__} from {meta_path.name}")
            return data["result"]

        # RECORD mode
        result = await fn(*args, **kwargs)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps({"fn": fn.__name__, "key": key, "result": result}, ensure_ascii=False),
            encoding="utf-8",
        )
        if result and output_path and Path(str(output_path)).exists():
            bin_dir = _cache_dir("http_bin")
            bin_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(output_path), bin_dir / f"{key}{Path(str(output_path)).suffix}")
        logger.debug(f"[fixture] recorded {fn.__name__} → {meta_path.name}")
        return result

    wrapper.__name__ = fn.__name__
    wrapper.__wrapped__ = fn
    return wrapper


# ---------------------------------------------------------------------------
# AI trace report
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TRACE_LOCK = Lock()
_TRACE_WORKSPACE: Path | None = None
_TRACE_COUNTER = itertools.count(1)


@dataclass(frozen=True)
class TraceRef:
    """Reserved trace identity plus consolidated report location."""

    trace_id: str
    json_path: Path
    html_rel_path: str
    workspace: Path
    run_id: str
    channel: str
    stage: str
    operation: str
    service: str
    model: str


def _slug(value: str) -> str:
    cleaned = _SLUG_RE.sub("_", value.lower()).strip("_")
    return cleaned or "trace"


def _report_json_path(workspace: Path) -> Path:
    return workspace / "reports" / "ai_trace_report.json"


def _trace_index(trace_id: str) -> int:
    prefix = trace_id.split("_", 1)[0]
    return int(prefix) if prefix.isdigit() else 0


def _ensure_counter(workspace: Path) -> None:
    global _TRACE_WORKSPACE, _TRACE_COUNTER
    if _TRACE_WORKSPACE == workspace:
        return

    highest = 0
    report_path = _report_json_path(workspace)
    if report_path.exists():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
            for trace in report.get("traces", []):
                highest = max(highest, _trace_index(str(trace.get("trace_id", ""))))
        except Exception:
            highest = 0

    _TRACE_WORKSPACE = workspace
    _TRACE_COUNTER = itertools.count(highest + 1)


def reserve_trace(
    *,
    operation: str,
    service: str,
    model: str,
) -> TraceRef | None:
    """Reserve a stable trace id for the active workspace/run."""
    tracker = costs.get_tracker()
    if tracker is None:
        return None

    labels = costs.current_billing_labels(operation)
    stage = labels.get("vf_stage", "unknown") or "unknown"
    workspace = tracker.workspace

    with _TRACE_LOCK:
        _ensure_counter(workspace)
        index = next(_TRACE_COUNTER)

    trace_id = f"{index:04d}_{_slug(operation)}"
    json_path = _report_json_path(workspace)
    html_rel_path = f"pipeline.html#{trace_id}"
    return TraceRef(
        trace_id=trace_id,
        json_path=json_path,
        html_rel_path=html_rel_path,
        workspace=workspace,
        run_id=tracker.run_id,
        channel=tracker.channel,
        stage=stage,
        operation=operation,
        service=service,
        model=model,
    )


def _default_report(trace_ref: TraceRef) -> dict[str, Any]:
    return {
        "run_id": trace_ref.run_id,
        "channel": trace_ref.channel,
        "generated_at": datetime.now().isoformat(),
        "traces": [],
    }


def _load_report(trace_ref: TraceRef) -> dict[str, Any]:
    if not trace_ref.json_path.exists():
        return _default_report(trace_ref)
    report = json.loads(trace_ref.json_path.read_text(encoding="utf-8"))
    report.setdefault("traces", [])
    report.setdefault("run_id", trace_ref.run_id)
    report.setdefault("channel", trace_ref.channel)
    return report


def _upsert_trace(report: dict[str, Any], payload: dict[str, Any]) -> None:
    traces = report.setdefault("traces", [])
    trace_id = payload["trace_id"]
    for index, trace in enumerate(traces):
        if trace.get("trace_id") == trace_id:
            traces[index] = payload
            break
    else:
        traces.append(payload)
        traces.sort(key=lambda item: _trace_index(str(item.get("trace_id", ""))))
    report["generated_at"] = datetime.now().isoformat()


def _metadata_rows(trace: dict[str, Any]) -> str:
    rows = []
    for label, key in [
        ("Trace ID", "trace_id"),
        ("Stage", "stage"),
        ("Operation", "operation"),
        ("Service", "service"),
        ("Model", "model"),
        ("Started At", "started_at"),
        ("Duration", "duration_seconds"),
        ("Status", "status"),
    ]:
        value = trace.get(key, "")
        if value == "":
            continue
        rows.append(
            f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
        )
    return "".join(rows)


def _block(title: str, content: str) -> str:
    if not content:
        return ""
    return (
        f"<section class=\"trace-block\"><h3>{html.escape(title)}</h3>"
        f"<pre>{html.escape(content)}</pre></section>"
    )


def _trace_card(trace: dict[str, Any]) -> str:
    request = trace.get("request", {})
    response = trace.get("response", {})
    response_json = response.get("json")
    response_json_text = (
        json.dumps(response_json, ensure_ascii=False, indent=2)
        if response_json is not None else ""
    )
    request_meta = {
        key: value
        for key, value in request.items()
        if key not in {"system_instruction", "prompt"}
    }
    response_meta = {
        key: value
        for key, value in response.items()
        if key not in {"text", "json", "error"}
    }
    status = str(trace.get("status", "ok"))
    trace_id = str(trace.get("trace_id", ""))
    return f"""
<details class="trace-card" id="{html.escape(trace_id, quote=True)}">
  <summary class="trace-card-header">
    <h2>{html.escape(trace_id)}</h2>
    <span class="pill pill-{html.escape(status, quote=True)}">{html.escape(status)}</span>
  </summary>
  <div class="trace-card-body">
    <table>{_metadata_rows(trace)}</table>
    {_block("Request Meta", json.dumps(request_meta, ensure_ascii=False, indent=2) if request_meta else "")}
    {_block("System Instruction", request.get("system_instruction", ""))}
    {_block("Prompt", request.get("prompt", ""))}
    {_block("Response Meta", json.dumps(response_meta, ensure_ascii=False, indent=2) if response_meta else "")}
    {_block("Response Text", response.get("text", ""))}
    {_block("Response JSON", response_json_text)}
    {_block("Error", response.get("error", ""))}
  </div>
</details>
"""


def render_embedded_html(report: dict[str, Any]) -> str:
    traces = report.get("traces", [])
    if not traces:
        return '<p class="artifact-empty">No AI traces captured for this run.</p>'
    nav_items = []
    cards = []
    for trace in traces:
        trace_id = str(trace.get("trace_id", "trace"))
        stage = str(trace.get("stage", "unknown"))
        operation = str(trace.get("operation", "unknown"))
        status = str(trace.get("status", "ok"))
        nav_items.append(
            "<a class=\"trace-link\" href=\"#"
            f"{html.escape(trace_id, quote=True)}\">"
            f"{html.escape(trace_id)}"
            f" <span>{html.escape(stage)} / {html.escape(operation)} / {html.escape(status)}</span>"
            "</a>"
        )
        cards.append(_trace_card(trace))

    return f"""
<div class="trace-report-meta">
  Run: {html.escape(str(report.get("run_id", "")))} |
  Channel: {html.escape(str(report.get("channel", "")))} |
  Generated: {html.escape(str(report.get("generated_at", "")))} |
  Calls: {len(traces)}
</div>
<nav class="trace-nav">
  {"".join(nav_items)}
</nav>
<div class="trace-report-cards">
  {"".join(cards)}
</div>
"""


def load_report(report_path: Path) -> dict[str, Any] | None:
    if not report_path.exists():
        return None
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report.setdefault("traces", [])
    return report


def write_trace(trace_ref: TraceRef, payload: dict[str, Any]) -> None:
    """Upsert one trace into the consolidated JSON report."""
    with _TRACE_LOCK:
        report = _load_report(trace_ref)
        _upsert_trace(report, payload)
        trace_ref.json_path.parent.mkdir(parents=True, exist_ok=True)
        trace_ref.json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )


def update_trace(trace_ref: TraceRef, patch: dict[str, Any]) -> None:
    """Update one trace entry inside the consolidated report."""
    with _TRACE_LOCK:
        if not trace_ref.json_path.exists():
            return
        report = _load_report(trace_ref)
        for trace in report.get("traces", []):
            if trace.get("trace_id") == trace_ref.trace_id:
                _deep_merge(trace, patch)
                report["generated_at"] = datetime.now().isoformat()
                trace_ref.json_path.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8",
                )
                return


def base_payload(
    trace_ref: TraceRef,
    *,
    started_at: datetime,
    duration_seconds: float | None = None,
    status: str = "ok",
) -> dict[str, Any]:
    return {
        "trace_id": trace_ref.trace_id,
        "run_id": trace_ref.run_id,
        "channel": trace_ref.channel,
        "stage": trace_ref.stage,
        "operation": trace_ref.operation,
        "service": trace_ref.service,
        "model": trace_ref.model,
        "started_at": started_at.isoformat(),
        "duration_seconds": round(duration_seconds, 3) if duration_seconds is not None else None,
        "status": status,
        "request": {},
        "response": {},
    }


def _deep_merge(target: dict[str, Any], patch: dict[str, Any]) -> None:
    for key, value in patch.items():
        if key in target and isinstance(target[key], dict) and isinstance(value, dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


# ---------------------------------------------------------------------------
# Shared client singleton
# ---------------------------------------------------------------------------

_client: genai.Client | None = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(
            vertexai=True,
            project=settings.google_project_id,
            location=settings.google_cloud_location,
            # Without an explicit timeout a stalled connection blocks the whole
            # pipeline indefinitely -- observed hanging a run for 25+ minutes on
            # a single vision call. A timeout turns that into a normal failure
            # that _call_model_with_retry can back off and retry.
            http_options=types.HttpOptions(
                timeout=settings.gemini_request_timeout_ms,
            ),
        )
    return _client


def _trace_suffix(trace_ref: TraceRef | None) -> str:
    return f' trace={trace_ref.html_rel_path}' if trace_ref else ""


#: In-process cache of deterministic model answers for one run.
#:
#: Deliberately process-local and unbounded-per-run rather than persisted: a
#: run is minutes long, the keys carry the model and prompt, and persisting it
#: would risk serving an answer produced by an older prompt or model version.
_GATEWAY_CACHE: dict[str, str] = {}


def reset_gateway_cache() -> None:
    """Clear the per-run answer cache. Called between runs and by tests."""
    _GATEWAY_CACHE.clear()


_RETRYABLE_STATUS_MARKERS = (
    "429",
    "RESOURCE_EXHAUSTED",
    "503",
    "UNAVAILABLE",
    "500",
    "INTERNAL",
    "504",
    "DEADLINE_EXCEEDED",
)
# Shared preview-model quota recovers in tens of seconds, not seconds, so the
# ladder is long and patient: ~4m of waiting beats losing a 25-minute run.
_MODEL_CALL_MAX_ATTEMPTS = 8
_MODEL_CALL_BASE_DELAY = 4.0   # seconds; doubled per attempt
_MODEL_CALL_MAX_DELAY = 60.0   # cap so late attempts stay responsive


def _is_retryable_model_error(exc: Exception) -> bool:
    """Whether a Gemini call failed for a reason that may clear on its own."""
    text = f"{type(exc).__name__}: {exc}"
    return any(marker in text for marker in _RETRYABLE_STATUS_MARKERS)


async def _call_model_with_retry(operation: str, call):
    """Await `call()`, retrying quota and transient server errors with backoff.

    Shared quota on preview models produces sporadic 429s; without this a
    single blip aborts a pipeline run that is otherwise minutes from done.
    """
    last_exc: Exception | None = None
    for attempt in range(1, _MODEL_CALL_MAX_ATTEMPTS + 1):
        try:
            return await call()
        except Exception as exc:
            last_exc = exc
            if attempt == _MODEL_CALL_MAX_ATTEMPTS or not _is_retryable_model_error(exc):
                raise
            delay = min(
                _MODEL_CALL_BASE_DELAY * (2 ** (attempt - 1)),
                _MODEL_CALL_MAX_DELAY,
            )
            logger.warning(
                f"[gemini] {operation} attempt {attempt}/"
                f"{_MODEL_CALL_MAX_ATTEMPTS} failed ({str(exc)[:120]}); "
                f"retrying in {delay:.0f}s"
            )
            await asyncio.sleep(delay)
    raise last_exc  # pragma: no cover - loop always returns or raises


async def _generate_text_response(
    prompt: str,
    *,
    system_instruction: str = "",
    model: str | None = None,
    temperature: float = 1.0,
    max_output_tokens: int = 8192,
    response_mime_type: str | None = None,
    operation_label: str | None = None,
) -> tuple[str, TraceRef | None]:
    """Generate text with Gemini and return raw text plus trace reference.

    The gateway gets two chances to change what happens here, and both are
    off by default:

      * `route` may pick a cheaper model that still meets the task's declared
        requirements. With smart routing disabled it returns the model the
        caller asked for, so this is a no-op.
      * `omniroute_available` may send the call through an OpenAI-compatible
        gateway rather than the Gemini SDK. Unset or half-configured, it is
        false and the SDK path runs.

    Everything below -- the retry ladder, the trace, the cost record -- is
    unchanged and applies to either transport.
    """
    from core import ai_gateway

    client = _get_client()
    model = model or settings.gemini_primary_model
    operation = operation_label or "generate_text"
    # The operation label doubles as the routing task: call sites already pass
    # a meaningful one, so no call site has to learn a new argument.
    model = ai_gateway.route(operation, requested=model)
    trace_ref = reserve_trace(
        operation=operation,
        service="generate_content",
        model=model,
    )

    prompt_preview = prompt[:80].replace("\n", " ")
    logger.info(
        f"[gemini] generate_text model={model} "
        f'max_tokens={max_output_tokens} prompt="{prompt_preview}…" ({len(prompt)} chars)'
        f"{_trace_suffix(trace_ref)}"
    )

    config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    if system_instruction:
        config.system_instruction = system_instruction
    if response_mime_type:
        config.response_mime_type = response_mime_type
    labels = costs.current_billing_labels(operation)
    if labels:
        config.labels = labels

    # Identical prompt, identical model, identical instructions -> identical
    # answer, so the second caller should not pay for it. Keyed on everything
    # that changes the result, so a changed prompt or an escalated model is a
    # different key rather than a stale hit.
    #
    # Only deterministic calls qualify: at a high temperature the model is
    # being asked for variety, and serving a cached answer would remove it.
    gateway_key = ""
    if temperature <= 0.4:
        gateway_key = ai_gateway.cache_key(
            operation, model, prompt, system_instruction,
            extra=f"{response_mime_type}|{max_output_tokens}",
        )
        cached = _GATEWAY_CACHE.get(gateway_key)
        if cached is not None and ai_gateway.cache_enabled():
            logger.info(
                f"[gateway] cache hit for {operation} "
                f"({len(cached)} chars, no request made)"
            )
            ai_gateway.LOG.add(
                ai_gateway.Attempt(operation, model, "cached"))
            return cached, None

    started_at = datetime.now()
    t0 = time.perf_counter()

    async def _call_gateway():
        """OmniRoute returns text, so it is wrapped to look like a response.

        Only the two fields the code below reads are provided. Usage metadata
        is absent because the gateway does not report Gemini's token counts,
        and inventing them would corrupt the cost record.
        """
        text = await ai_gateway.omniroute_generate(
            prompt,
            model=model,
            system_instruction=system_instruction,
            response_json=response_mime_type == "application/json",
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        return SimpleNamespace(text=text, usage_metadata=None)

    try:
        if ai_gateway.omniroute_available():
            response = await _call_model_with_retry(operation, _call_gateway)
        else:
            response = await _call_model_with_retry(
                operation,
                lambda: client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=config,
                ),
            )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        if trace_ref:
            payload = base_payload(
                trace_ref,
                started_at=started_at,
                duration_seconds=elapsed,
                status="error",
            )
            payload["request"] = {
                "system_instruction": system_instruction,
                "prompt": prompt,
                "temperature": temperature,
                "max_output_tokens": max_output_tokens,
                "response_mime_type": response_mime_type,
            }
            payload["response"] = {
                "error": str(exc),
            }
            write_trace(trace_ref, payload)
        raise

    elapsed = time.perf_counter() - t0
    usage = response.usage_metadata
    if usage is not None:
        costs.record_generate_content_cost(
            model=model,
            usage_metadata=usage,
            operation=operation,
        )
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage, "candidates_token_count", 0) or 0
    logger.info(
        f"[gemini] generate_text done — {elapsed:.1f}s, "
        f"{prompt_tokens} in + {output_tokens} out tokens, "
        f"{len(response.text)} chars"
        f"{_trace_suffix(trace_ref)}"
    )
    if trace_ref:
        payload = base_payload(
            trace_ref,
            started_at=started_at,
            duration_seconds=elapsed,
            status="ok",
        )
        payload["request"] = {
            "system_instruction": system_instruction,
            "prompt": prompt,
            "temperature": temperature,
            "max_output_tokens": max_output_tokens,
            "response_mime_type": response_mime_type,
        }
        payload["response"] = {
            "text": response.text,
            "prompt_token_count": prompt_tokens or None,
            "output_token_count": output_tokens or None,
        }
        write_trace(trace_ref, payload)
    if gateway_key and ai_gateway.cache_enabled():
        _GATEWAY_CACHE[gateway_key] = response.text
    return response.text, trace_ref


# ---------------------------------------------------------------------------
# Gemini — text generation
# ---------------------------------------------------------------------------

@cached_text
async def generate_text(
    prompt: str,
    *,
    system_instruction: str = "",
    model: str | None = None,
    temperature: float = 1.0,
    max_output_tokens: int = 8192,
    response_mime_type: str | None = None,
    operation_label: str | None = None,
) -> str:
    """Generate text with Gemini. Returns the raw text response."""
    text, _trace_ref = await _generate_text_response(
        prompt,
        system_instruction=system_instruction,
        model=model,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type=response_mime_type,
        operation_label=operation_label,
    )
    return text


async def generate_json(
    prompt: str,
    *,
    system_instruction: str = "",
    model: str | None = None,
    temperature: float = 1.0,
    max_output_tokens: int = 8192,
    operation_label: str | None = None,
) -> dict | list:
    """Generate structured JSON from Gemini."""
    text, trace_ref = await _generate_text_response(
        prompt,
        system_instruction=system_instruction,
        model=model,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json",
        operation_label=operation_label or "generate_json",
    )
    parsed = json.loads(text)
    if trace_ref:
        update_trace(trace_ref, {"response": {"json": parsed}})
    return parsed


# ---------------------------------------------------------------------------
# Gemini — web-grounded research
# ---------------------------------------------------------------------------

@cached_json
async def research_with_search(
    prompt: str,
    *,
    schema_prompt: str = "",
    system_instruction: str = "",
    model: str | None = None,
    temperature: float = 0.2,
    max_output_tokens: int = 8192,
    operation_label: str | None = None,
) -> dict:
    """Answer a prompt with Google Search grounding, returning parsed JSON.

    News scripts go stale fast: without live search the model happily writes
    "a possible transfer" about a deal that completed last week. Grounding ties
    the claims to current sources, and the returned `sources`/`queries` make
    the provenance auditable in the run's AI trace.

    Done in two passes on purpose. Asking one grounded call to "respond ONLY
    with JSON" reliably suppresses the search tool -- the model just answers
    from memory and returns an empty grounding_metadata, which is worse than
    useless because the output *looks* researched. So pass 1 researches in
    prose with the tool enabled, and pass 2 (no tools) structures that text.
    """
    client = _get_client()
    model = model or settings.gemini_research_model
    operation = operation_label or "research_with_search"
    trace_ref = reserve_trace(
        operation=operation,
        service="generate_content",
        model=model,
    )

    logger.info(
        f"[gemini] research_with_search model={model} "
        f"prompt=\"{prompt[:70].replace(chr(10), ' ')}…\" ({len(prompt)} chars)"
        f"{_trace_suffix(trace_ref)}"
    )

    labels = costs.current_billing_labels(operation)

    # Pass 1: research in prose, with search enabled.
    research_config = types.GenerateContentConfig(
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        tools=[types.Tool(google_search=types.GoogleSearch())],
    )
    if system_instruction:
        research_config.system_instruction = system_instruction
    if labels:
        research_config.labels = labels
    config = research_config

    started_at = datetime.now()
    t0 = time.perf_counter()
    try:
        response = await _call_model_with_retry(
            operation,
            lambda: client.aio.models.generate_content(
                model=model,
                contents=prompt,
                config=config,
            ),
        )
    except Exception as exc:
        if trace_ref:
            payload = base_payload(
                trace_ref, started_at=started_at,
                duration_seconds=time.perf_counter() - t0, status="error",
            )
            payload["request"] = {"prompt": prompt}
            payload["response"] = {"error": str(exc)}
            write_trace(trace_ref, payload)
        raise

    elapsed = time.perf_counter() - t0
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        costs.record_generate_content_cost(
            model=model, usage_metadata=usage, operation=operation,
        )

    text = (response.text or "").strip()

    # Pass 2: turn the grounded prose into the requested JSON. No tools here,
    # so nothing competes with the structured-output instruction.
    structure_config = types.GenerateContentConfig(
        temperature=0.0,
        # Headroom: a truncated response is unparseable JSON, and the findings
        # plus the schema can be long.
        max_output_tokens=max(max_output_tokens, 16384),
        response_mime_type="application/json",
    )
    if labels:
        structure_config.labels = labels
    structured = await _call_model_with_retry(
        f"{operation}_structure",
        lambda: client.aio.models.generate_content(
            model=model,
            contents=(
                "Convert these research findings into the exact JSON object the "
                "instructions below ask for. Use ONLY what the findings state; "
                "do not add facts.\n\n"
                f"=== FINDINGS ===\n{text}\n\n"
                f"=== REQUIRED OUTPUT ===\n{schema_prompt or prompt}"
            ),
            config=structure_config,
        ),
    )
    if getattr(structured, "usage_metadata", None) is not None:
        costs.record_generate_content_cost(
            model=model,
            usage_metadata=structured.usage_metadata,
            operation=f"{operation}_structure",
        )
    # Structuring is the fragile half: the model can truncate or fence the JSON
    # oddly. The grounded findings are the valuable part and were already paid
    # for, so a structuring failure degrades to the prose brief instead of
    # throwing away verified research.
    try:
        parsed = _parse_fenced_json((structured.text or "").strip())
    except ValueError as exc:
        logger.warning(
            f"[gemini] {operation}: could not structure the grounded findings "
            f"({exc}); falling back to the raw research text"
        )
        parsed = {
            "headline_status": text[:600],
            "latest_development": "",
            "verified_facts": [],
            "key_entities": [],
            "structuring_failed": True,
        }

    # Provenance from the grounding metadata, for the trace and the script.
    queries: list[str] = []
    sources: list[dict[str, str]] = []
    try:
        meta = response.candidates[0].grounding_metadata
        queries = list(getattr(meta, "web_search_queries", None) or [])
        for chunk in getattr(meta, "grounding_chunks", None) or []:
            web = getattr(chunk, "web", None)
            if web is not None:
                sources.append({
                    "title": getattr(web, "title", "") or "",
                    "uri": getattr(web, "uri", "") or "",
                })
    except (AttributeError, IndexError):
        pass

    if isinstance(parsed, dict):
        # Overwrite rather than setdefault: provenance comes from the grounded
        # pass, not from whatever the structuring pass invented.
        parsed["search_queries"] = queries
        parsed["sources"] = sources
        parsed["grounded"] = bool(queries or sources)

    if not (queries or sources):
        logger.warning(
            f"[gemini] {operation} returned no grounding metadata — the answer "
            f"came from training data and may be out of date"
        )

    logger.info(
        f"[gemini] research_with_search done — {elapsed:.1f}s, "
        f"{len(queries)} search(es), {len(sources)} source(s)"
        f"{_trace_suffix(trace_ref)}"
    )
    if trace_ref:
        payload = base_payload(
            trace_ref, started_at=started_at,
            duration_seconds=elapsed, status="ok",
        )
        payload["request"] = {
            "system_instruction": system_instruction,
            "prompt": prompt,
        }
        payload["response"] = {
            "text": text,
            "json": parsed,
            "search_queries": queries,
            "sources": sources,
        }
        write_trace(trace_ref, payload)
    return parsed


def _parse_fenced_json(text: str) -> dict:
    """Pull a JSON object out of a possibly fenced/prose-wrapped response."""
    candidate = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", candidate, re.S)
    if fence:
        candidate = fence.group(1).strip()
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start != -1 and end > start:
            candidate = candidate[start:end + 1]
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"Grounded research did not return JSON: {text[:300]}"
        ) from e
    if not isinstance(parsed, dict):
        raise ValueError(f"Grounded research returned {type(parsed).__name__}, not an object")
    return parsed


# ---------------------------------------------------------------------------
# Gemini — vision review
# ---------------------------------------------------------------------------

@cached_json
async def review_with_vision(
    prompt: str,
    image_paths: list[Path],
    *,
    system_instruction: str = "",
    model: str | None = None,
    operation_label: str | None = None,
) -> dict:
    """Send images + prompt to Gemini for vision-based review. Returns parsed JSON."""
    client = _get_client()
    model = model or settings.gemini_review_model
    operation = operation_label or "review_with_vision"
    trace_ref = reserve_trace(
        operation=operation,
        service="generate_content",
        model=model,
    )

    prompt_preview = prompt[:80].replace("\n", " ")
    logger.info(
        f"[gemini] review_with_vision model={model} "
        f'images={len(image_paths)} prompt="{prompt_preview}…" ({len(prompt)} chars)'
        f"{_trace_suffix(trace_ref)}"
    )

    parts: list[types.Part] = []
    for img_path in image_paths:
        img_bytes = img_path.read_bytes()
        suffix = img_path.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "image/jpeg")
        parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))

    parts.append(types.Part.from_text(text=prompt))

    config = types.GenerateContentConfig(
        temperature=0.3,
        max_output_tokens=8192,
        response_mime_type="application/json",
    )
    if system_instruction:
        config.system_instruction = system_instruction
    labels = costs.current_billing_labels(operation)
    if labels:
        config.labels = labels

    started_at = datetime.now()
    t0 = time.perf_counter()
    try:
        response = await _call_model_with_retry(
            operation,
            lambda: client.aio.models.generate_content(
                model=model,
                contents=parts,
                config=config,
            ),
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        if trace_ref:
            payload = base_payload(
                trace_ref,
                started_at=started_at,
                duration_seconds=elapsed,
                status="error",
            )
            payload["request"] = {
                "system_instruction": system_instruction,
                "prompt": prompt,
                "image_paths": [str(path) for path in image_paths],
                "response_mime_type": "application/json",
            }
            payload["response"] = {"error": str(exc)}
            write_trace(trace_ref, payload)
        raise
    elapsed = time.perf_counter() - t0
    usage = response.usage_metadata
    if usage is not None:
        costs.record_generate_content_cost(
            model=model,
            usage_metadata=usage,
            operation=operation,
        )
    prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
    output_tokens = getattr(usage, "candidates_token_count", 0) or 0
    logger.info(
        f"[gemini] review_with_vision done — {elapsed:.1f}s, "
        f"{prompt_tokens} in + {output_tokens} out tokens"
        f"{_trace_suffix(trace_ref)}"
    )
    text = response.text
    parsed_json = None
    repaired = False
    try:
        parsed_json = json.loads(text)
    except json.JSONDecodeError as original_err:
        # Attempt basic repair for truncated JSON responses
        repaired_text = text.strip()

        # Strip markdown fences if present
        if repaired_text.startswith("```"):
            first_nl = repaired_text.find("\n")
            repaired_text = repaired_text[first_nl + 1 :] if first_nl != -1 else repaired_text[3:]
            if repaired_text.endswith("```"):
                repaired_text = repaired_text[:-3]
            repaired_text = repaired_text.strip()

        # Close unterminated string
        if repaired_text.count('"') % 2 != 0:
            repaired_text += '"'

        # Close open brackets/braces
        for open_ch, close_ch in [("[", "]"), ("{", "}")]:
            deficit = repaired_text.count(open_ch) - repaired_text.count(close_ch)
            if deficit > 0:
                repaired_text += close_ch * deficit

        try:
            logger.warning("Repaired truncated JSON from vision review")
            parsed_json = json.loads(repaired_text)
            repaired = True
        except json.JSONDecodeError:
            raise original_err
    if trace_ref:
        payload = base_payload(
            trace_ref,
            started_at=started_at,
            duration_seconds=elapsed,
            status="ok",
        )
        payload["request"] = {
            "system_instruction": system_instruction,
            "prompt": prompt,
            "image_paths": [str(path) for path in image_paths],
            "response_mime_type": "application/json",
        }
        payload["response"] = {
            "text": text,
            "json": parsed_json,
            "json_repaired": repaired,
            "prompt_token_count": prompt_tokens or None,
            "output_token_count": output_tokens or None,
        }
        write_trace(trace_ref, payload)
    return parsed_json


# ---------------------------------------------------------------------------
# Gemini — image generation (Nano Banana)
# ---------------------------------------------------------------------------

@cached_file("image")
async def generate_image_gemini(
    prompt: str,
    output_path: Path,
    *,
    model: str | None = None,
    reference_image: Path | None = None,
    aspect_ratio: str = "16:9",
    image_size: str = "1K",
    operation_label: str | None = None,
) -> Path | None:
    """Generate an image with Gemini (Nano Banana) and save to output_path.

    If reference_image is provided, it is sent as context so the model can
    enhance or riff on it rather than generating from scratch.

    Returns the path on success, None on failure.
    """
    client = _get_client()
    model = model or settings.gemini_image_model
    operation = operation_label or "generate_image_gemini"
    trace_ref = reserve_trace(
        operation=operation,
        service="generate_content_image",
        model=model,
    )

    prompt_preview = prompt[:80].replace("\n", " ")
    logger.info(
        f"[gemini] generate_image_gemini model={model} "
        f"ref={'yes' if reference_image else 'no'} "
        f'prompt="{prompt_preview}…" ({len(prompt)} chars)'
        f"{_trace_suffix(trace_ref)}"
    )

    contents: list = []
    if reference_image and reference_image.exists():
        img_bytes = reference_image.read_bytes()
        suffix = reference_image.suffix.lower()
        mime = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".webp": "image/webp",
        }.get(suffix, "image/jpeg")
        contents.append(types.Part.from_bytes(data=img_bytes, mime_type=mime))
    contents.append(prompt)

    started_at = datetime.now()
    try:
        t0 = time.perf_counter()
        # Through the same retry ladder as text generation.
        #
        # This call used to go straight to the SDK, so a 429 failed the beat
        # instantly -- observed as four generations failing in the same second
        # with no backoff, which cost a run its images and then its review
        # gate. Image quota is the tightest of any model here, so it is the
        # path that most needs the wait.
        response = await _call_model_with_retry(
            operation,
            lambda: client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                    image_config=types.ImageConfig(
                        aspect_ratio=aspect_ratio,
                        image_size=image_size,
                    ),
                    labels=costs.current_billing_labels(operation),
                ),
            ),
        )
        elapsed = time.perf_counter() - t0
        generated_images = 0

        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(part.inline_data.data)
                generated_images += 1
                usage = getattr(response, "usage_metadata", None)
                if usage is not None:
                    costs.record_generate_content_cost(
                        model=model,
                        usage_metadata=usage,
                        operation=operation,
                        service="generate_content_image",
                        generated_images=generated_images,
                    )
                logger.info(
                    f"[gemini] generate_image_gemini done — {elapsed:.1f}s → {output_path.name}"
                    f"{_trace_suffix(trace_ref)}"
                )
                if trace_ref:
                    payload = base_payload(
                        trace_ref,
                        started_at=started_at,
                        duration_seconds=elapsed,
                        status="ok",
                    )
                    payload["request"] = {
                        "prompt": prompt,
                        "reference_image": str(reference_image) if reference_image else None,
                        "aspect_ratio": aspect_ratio,
                        "image_size": image_size,
                        "output_path": str(output_path),
                    }
                    payload["response"] = {
                        "generated_images": generated_images,
                        "output_path": str(output_path),
                    }
                    write_trace(trace_ref, payload)
                return output_path

        usage = getattr(response, "usage_metadata", None)
        if usage is not None:
            costs.record_generate_content_cost(
                model=model,
                usage_metadata=usage,
                operation=operation,
                service="generate_content_image",
                generated_images=generated_images or None,
            )
        logger.warning(
            f"[gemini] generate_image_gemini done — {elapsed:.1f}s, no image returned"
            f"{_trace_suffix(trace_ref)}"
        )
        if trace_ref:
            payload = base_payload(
                trace_ref,
                started_at=started_at,
                duration_seconds=elapsed,
                status="empty",
            )
            payload["request"] = {
                "prompt": prompt,
                "reference_image": str(reference_image) if reference_image else None,
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
                "output_path": str(output_path),
            }
            payload["response"] = {
                "generated_images": generated_images,
            }
            write_trace(trace_ref, payload)
        return None

    except Exception as e:
        if trace_ref:
            payload = base_payload(
                trace_ref,
                started_at=started_at,
                status="error",
            )
            payload["request"] = {
                "prompt": prompt,
                "reference_image": str(reference_image) if reference_image else None,
                "aspect_ratio": aspect_ratio,
                "image_size": image_size,
                "output_path": str(output_path),
            }
            payload["response"] = {"error": str(e)}
            write_trace(trace_ref, payload)
        logger.error(f"Gemini image generation failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Scene visuals — generated only where real photography could not be found
# ---------------------------------------------------------------------------


async def generate_scene_image(
    prompt: str,
    output_path: Path,
    *,
    model: str | None = None,
    aspect_ratio: str = "16:9",
    image_size: str = "1K",
    target_size: tuple[int, int] | None = None,
    operation_label: str | None = None,
    reference_image: Path | None = None,
) -> Path | None:
    """Generate one scene still, on whichever generator the channel configures.

    A single entry point so the *channel* decides its generator and the
    sourcing code does not have to know which one it got. `model` comes from
    `image_sourcing.generated_fallback_model`, so Horror and True Stories
    reach fal's FLUX Schnell and every other channel keeps the Gemini path
    byte for byte.

    Returns the path on success and None on failure, matching
    `generate_image_gemini`, so the caller's existing "the beat is still
    unsourced" handling applies unchanged whichever generator ran.

    `reference_image` is how Animated Stories keeps one character across
    twenty-one scenes: the character's reference sheet goes in with the prompt
    so the model can see who it is drawing rather than reconstruct them from a
    paragraph. Only the Gemini generator accepts one -- fal's Schnell is
    text-to-image -- so a caller with a sheet in hand should choose a
    reference-capable model; passing one to fal is a no-op, not an error.
    """
    if str(model or "").startswith("fal-ai/"):
        if reference_image:
            logger.debug(
                f"{model} cannot take a reference image; "
                f"generating {output_path.name} from the prompt alone"
            )
        return await _generate_scene_image_fal(
            prompt,
            output_path,
            model=str(model),
            aspect_ratio=aspect_ratio,
            target_size=target_size,
            operation_label=operation_label or "generate_scene_image",
        )
    # Module-global lookup on purpose: tests monkeypatch
    # `clients.generate_image_gemini`, and the Gemini path must stay exactly
    # what it was for every channel that has not opted in.
    return await generate_image_gemini(
        prompt,
        output_path,
        model=model,
        aspect_ratio=aspect_ratio,
        image_size=image_size,
        operation_label=operation_label,
        reference_image=reference_image,
    )


async def _generate_scene_image_fal(
    prompt: str,
    output_path: Path,
    *,
    model: str,
    aspect_ratio: str,
    target_size: tuple[int, int] | None,
    operation_label: str,
) -> Path | None:
    """One generated scene still from fal, traced and priced like the rest."""
    from core.providers.scene_images import (
        FluxSchnellProvider,
        dimensions_for,
        megapixels,
    )

    provider = FluxSchnellProvider()
    status = provider.status()
    if not status.usable:
        logger.error(f"[scene] fal unavailable: {status.reason}")
        return None

    width, height = dimensions_for(aspect_ratio, target_size or (0, 0))
    trace_ref = reserve_trace(
        operation=operation_label,
        service="generate_content_image",
        model=provider.MODEL,
    )
    logger.info(
        f"[scene] {provider.MODEL} {width}x{height} "
        f"prompt=\"{prompt[:70]}…\"{_trace_suffix(trace_ref)}"
    )

    started_at = datetime.now()
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(provider.TIMEOUT, connect=10.0)
        ) as http:
            image_bytes = await provider.generate(
                prompt, client=http, width=width, height=height
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(image_bytes)
        elapsed = time.perf_counter() - t0

        # fal bills per megapixel rounded up; the catalog rate is stated per
        # *image* at the sizes this pipeline requests, so one image is
        # recorded as one image. Counting megapixels here would price
        # correctly but report three images for every one generated, and
        # `images_generated` is a number people read.
        billed = megapixels(width, height)
        costs.record_generate_content_cost(
            model=provider.MODEL,
            usage_metadata=SimpleNamespace(),
            operation=operation_label,
            service="generate_content_image",
            provider="fal_ai",
            generated_images=1,
        )
        logger.info(
            f"[scene] done — {elapsed:.1f}s → {output_path.name} "
            f"({len(image_bytes):,} bytes, {billed}MP billed)"
            f"{_trace_suffix(trace_ref)}"
        )
        if trace_ref:
            payload = base_payload(
                trace_ref,
                started_at=started_at,
                duration_seconds=elapsed,
                status="ok",
            )
            payload["request"] = {
                "provider": provider.name,
                "model": provider.MODEL,
                "prompt": prompt,
                "width": width,
                "height": height,
                "aspect_ratio": aspect_ratio,
                "output_path": str(output_path),
            }
            payload["response"] = {
                "output_path": str(output_path),
                "bytes": len(image_bytes),
                "billed_megapixels": billed,
            }
            write_trace(trace_ref, payload)
        return output_path

    except Exception as e:
        if trace_ref:
            payload = base_payload(trace_ref, started_at=started_at, status="error")
            payload["request"] = {
                "provider": provider.name,
                "model": provider.MODEL,
                "prompt": prompt,
                "output_path": str(output_path),
            }
            payload["response"] = {"error": str(e)}
            write_trace(trace_ref, payload)
        logger.error(f"Scene image generation failed: {e}")
        return None


# ---------------------------------------------------------------------------
# Thumbnails — edited from a real photograph
# ---------------------------------------------------------------------------


@cached_file("image")
async def edit_thumbnail_image(
    prompt: str,
    output_path: Path,
    *,
    source_images: list[Path],
    aspect_ratio: str = "16:9",
    operation_label: str | None = None,
) -> Path | None:
    """Compose a thumbnail by editing real photographs.

    Deliberately a separate entry point from `generate_image_gemini`: scene
    visuals and thumbnails are different jobs with different truth rules, and
    keeping them apart is what stops a change to one from silently altering
    the other. Nothing in scene sourcing reaches this function.

    Returns the path on success, None on failure, exactly like the image
    generator beside it -- so the thumbnail stage's existing retry and review
    gate behave the same whichever produced the picture.
    """
    from core.providers.thumbnails import FalGeminiFlashEditProvider

    provider = FalGeminiFlashEditProvider()
    status = provider.status()
    if not status.usable:
        logger.error(f"[thumbnail] fal unavailable: {status.reason}")
        return None

    operation = operation_label or "thumbnail_edit"
    trace_ref = reserve_trace(
        operation=operation,
        service="generate_content_image",
        model=provider.MODEL,
    )
    bases = [Path(p).name for p in source_images]
    logger.info(
        f"[thumbnail] {provider.MODEL} editing {len(source_images)} source "
        f"image(s) [{', '.join(bases)}] ar={aspect_ratio}"
        f"{_trace_suffix(trace_ref)}"
    )

    started_at = datetime.now()
    t0 = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(provider.TIMEOUT, connect=10.0)
        ) as http:
            image_bytes = await provider.edit(
                prompt, list(source_images), client=http, aspect_ratio=aspect_ratio
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(image_bytes)
        elapsed = time.perf_counter() - t0

        # Priced per image rather than per token, so the Gemini token model
        # does not describe it. Recorded as one generated image and left for
        # the pricing table; a missing entry is reported rather than counted
        # as free.
        costs.record_generate_content_cost(
            model=provider.MODEL,
            usage_metadata=SimpleNamespace(),
            operation=operation,
            service="generate_content_image",
            provider="fal_ai",
            generated_images=1,
        )
        logger.info(
            f"[thumbnail] done — {elapsed:.1f}s → {output_path.name} "
            f"({len(image_bytes):,} bytes){_trace_suffix(trace_ref)}"
        )
        if trace_ref:
            payload = base_payload(
                trace_ref,
                started_at=started_at,
                duration_seconds=elapsed,
                status="ok",
            )
            payload["request"] = {
                "provider": provider.name,
                "model": provider.MODEL,
                "prompt": prompt,
                "source_images": [str(p) for p in source_images],
                "aspect_ratio": aspect_ratio,
                "output_path": str(output_path),
            }
            payload["response"] = {
                "output_path": str(output_path),
                "bytes": len(image_bytes),
            }
            write_trace(trace_ref, payload)
        return output_path

    except Exception as e:
        if trace_ref:
            payload = base_payload(trace_ref, started_at=started_at, status="error")
            payload["request"] = {
                "provider": provider.name,
                "model": provider.MODEL,
                "prompt": prompt,
                "source_images": [str(p) for p in source_images],
                "output_path": str(output_path),
            }
            payload["response"] = {"error": str(e)}
            write_trace(trace_ref, payload)
        logger.error(f"Thumbnail edit failed: {e}")
        return None


# ---------------------------------------------------------------------------
# TTS — speech generation
# ---------------------------------------------------------------------------

#: Gemini TTS returns mono 16-bit PCM at 24 kHz. ElevenLabs narration is
#: normalised to exactly that, because `_concat_wavs` joins sections with the
#: `wave` module and refuses a file whose parameters differ from the first.
#: A run that mixes providers across sections still concatenates cleanly.
_NARRATION_SAMPLE_RATE = 24000


async def _speak_elevenlabs(
    text: str,
    output_path: Path,
    *,
    voice_id: str,
    language: str,
    voice_label: str,
    operation_label: str,
) -> Path | None:
    """One section of narration, in one named ElevenLabs voice.

    Returns a WAV path so the rest of the audio stage cannot tell which
    provider spoke, or None on failure so the caller's existing retry applies.
    """
    from core.providers.narration import ElevenLabsNarrationProvider

    provider = ElevenLabsNarrationProvider()
    status = provider.status()
    if not status.usable:
        logger.error(f"[tts] ElevenLabs narration unavailable: {status.reason}")
        return None

    trace_ref = reserve_trace(
        operation=operation_label,
        service="tts",
        model=provider.MODEL,
    )
    logger.info(
        f"[tts] elevenlabs model={provider.MODEL} voice={voice_label or voice_id} "
        f"lang={language} text={len(text.split())} words"
        f"{_trace_suffix(trace_ref)}"
    )

    started_at = datetime.now()
    t0 = time.perf_counter()
    mp3_path = output_path.with_suffix(".mp3")
    wav_path = output_path.with_suffix(".wav")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(180.0, connect=10.0)) as http:
            audio = await provider.speak(text, voice_id=voice_id, client=http)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        mp3_path.write_bytes(audio)

        result = subprocess.run(
            [
                settings.ffmpeg_path, "-y",
                "-i", str(mp3_path),
                "-ac", "1",
                "-ar", str(_NARRATION_SAMPLE_RATE),
                "-c:a", "pcm_s16le",
                str(wav_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0 or not wav_path.exists():
            raise RuntimeError(
                f"ffmpeg could not decode the narration MP3: "
                f"{(result.stderr or '').strip()[:300]}"
            )
        mp3_path.unlink(missing_ok=True)

        elapsed = time.perf_counter() - t0
        audio_seconds = wav_path.stat().st_size / (_NARRATION_SAMPLE_RATE * 2)
        # Billed per character, not per token, so the Gemini TTS cost model
        # does not describe this. Recorded as characters and left for the
        # pricing table to price, rather than reported as free.
        costs.record_tts_cost(
            model=provider.MODEL,
            prompt_tokens=len(text),
            audio_seconds=audio_seconds,
            operation=operation_label,
        )
        logger.info(
            f"[tts] elevenlabs done — {elapsed:.1f}s → {wav_path.name} "
            f"({audio_seconds:.1f}s audio){_trace_suffix(trace_ref)}"
        )
        if trace_ref:
            payload = base_payload(
                trace_ref,
                started_at=started_at,
                duration_seconds=elapsed,
                status="ok",
            )
            payload["request"] = {
                "provider": provider.name,
                "voice_id": voice_id,
                "voice_label": voice_label,
                "language": language,
                "characters": len(text),
                "output_path": str(wav_path),
            }
            payload["response"] = {
                "output_path": str(wav_path),
                "audio_seconds": round(audio_seconds, 3),
                "sample_rate": _NARRATION_SAMPLE_RATE,
            }
            write_trace(trace_ref, payload)
        return wav_path

    except Exception as e:
        mp3_path.unlink(missing_ok=True)
        if trace_ref:
            payload = base_payload(trace_ref, started_at=started_at, status="error")
            payload["request"] = {
                "provider": "elevenlabs_narration",
                "voice_id": voice_id,
                "language": language,
                "output_path": str(wav_path),
            }
            payload["response"] = {"error": str(e)}
            write_trace(trace_ref, payload)
        logger.error(f"ElevenLabs narration failed: {e}")
        return None


@cached_file("speech")
async def generate_speech(
    text: str,
    output_path: Path,
    *,
    voice_prompt: str = "Read this aloud in a warm, clear narrator voice.",
    language: str = "en-US",
    voice_name: str = "Charon",
    model: str | None = None,
    operation_label: str | None = None,
    provider: str = "",
    voice_id: str = "",
) -> Path | None:
    """Generate speech audio from text.

    Gemini TTS by default: the voice characteristics are controlled via the
    voice_prompt — describe the voice you want (tone, pace, emotion, accent)
    — and the base voice is selected via voice_name ("Charon", "Kore", …).

    A channel whose voice config sets `provider: "elevenlabs"` and a
    `voice_id` is narrated by that one ElevenLabs voice instead. There is no
    voice_prompt in that path: an ElevenLabs voice *is* the performance, and a
    prompt describing a different one would be silently ignored.

    Returns the output path on success, None on failure.
    """
    if str(provider or "").strip().lower().startswith("eleven"):
        if str(voice_id or "").strip():
            return await _speak_elevenlabs(
                text,
                output_path,
                voice_id=voice_id,
                language=language,
                voice_label=voice_name,
                operation_label=operation_label or "generate_speech",
            )
        # Configured for ElevenLabs but with no voice to speak in. Gemini is
        # the documented default and narrates correctly, so falling through
        # costs a different voice; refusing would cost the whole run.
        logger.warning(
            "[tts] channel asks for ElevenLabs narration but no voice_id is "
            "configured; narrating with Gemini TTS instead"
        )

    client = _get_client()
    model = model or settings.gemini_tts_model
    operation = operation_label or "generate_speech"
    trace_ref = reserve_trace(
        operation=operation,
        service="tts",
        model=model,
    )

    word_count = len(text.split())
    logger.info(
        f"[tts] generate_speech model={model} "
        f"voice={voice_name} lang={language} text={word_count} words"
        f"{_trace_suffix(trace_ref)}"
    )

    full_prompt = f"{voice_prompt}\n\nText to read ({language}):\n{text}"

    started_at = datetime.now()
    try:
        t0 = time.perf_counter()
        response = await _call_model_with_retry(
            operation,
            lambda: client.aio.models.generate_content(
                model=model,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    response_modalities=["AUDIO"],
                    labels=costs.current_billing_labels(operation),
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=voice_name,
                            )
                        )
                    ),
                ),
            ),
        )
        elapsed = time.perf_counter() - t0

        # Extract audio data from response
        audio_data = None
        sample_rate = 24000  # default
        for part in response.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("audio/"):
                audio_data = part.inline_data.data
                # Parse sample rate from mime type (e.g. "audio/L16;codec=pcm;rate=24000")
                mime = part.inline_data.mime_type
                if "rate=" in mime:
                    sample_rate = int(mime.split("rate=")[1].split(";")[0])
                break

        if audio_data is None:
            logger.warning("TTS returned no audio data")
            return None

        output_path.parent.mkdir(parents=True, exist_ok=True)

        # Gemini TTS returns raw PCM L16 (16-bit signed, mono).
        # Wrap it in a proper WAV header so FFmpeg/MoviePy can read it.
        wav_path = output_path.with_suffix(".wav")
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit = 2 bytes
            wf.setframerate(sample_rate)
            wf.writeframes(audio_data)

        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        audio_seconds = len(audio_data) / (sample_rate * 2)
        costs.record_tts_cost(
            model=model,
            prompt_tokens=prompt_tokens,
            audio_seconds=audio_seconds,
            operation=operation,
        )
        logger.info(
            f"[tts] generate_speech done — {elapsed:.1f}s → {wav_path.name}"
            f"{_trace_suffix(trace_ref)}"
        )
        if trace_ref:
            payload = base_payload(
                trace_ref,
                started_at=started_at,
                duration_seconds=elapsed,
                status="ok",
            )
            payload["request"] = {
                "voice_prompt": voice_prompt,
                "language": language,
                "voice_name": voice_name,
                "prompt": full_prompt,
                "output_path": str(wav_path),
            }
            payload["response"] = {
                "output_path": str(wav_path),
                "prompt_token_count": prompt_tokens or None,
                "audio_seconds": round(audio_seconds, 3),
                "sample_rate": sample_rate,
            }
            write_trace(trace_ref, payload)
        return wav_path

    except Exception as e:
        if trace_ref:
            payload = base_payload(
                trace_ref,
                started_at=started_at,
                status="error",
            )
            payload["request"] = {
                "voice_prompt": voice_prompt,
                "language": language,
                "voice_name": voice_name,
                "prompt": full_prompt,
                "output_path": str(output_path.with_suffix(".wav")),
            }
            payload["response"] = {"error": str(e)}
            write_trace(trace_ref, payload)
        logger.error(f"TTS generation failed: {e}")
        return None
