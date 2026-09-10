"""Every setting the Clipping screen shows must reach the worker.

The screen, the API route and the worker each name the same options in their
own language. Nothing forces them to agree, so a renamed id would silently
fall back to a default: the control would still move, the clip would ignore
it, and no test would fail.

These read the actual files and compare, so a drift in any one of the three
breaks the build rather than the product.

Offline: no network, no ffmpeg, no paid call. Text in, assertions out.
"""

import json
import re
from pathlib import Path

import pytest

from viral import clipping as clip

ROOT = Path(__file__).resolve().parents[1]
TS_LIB = ROOT / "web" / "lib" / "clipping.ts"
TS_UI = ROOT / "web" / "components" / "reels" / "Clipping.tsx"
API_ROUTE = ROOT / "web" / "app" / "api" / "reels" / "clips" / "route.ts"
UPLOAD_ROUTE = ROOT / "web" / "app" / "api" / "reels" / "clips" / "upload" / "route.ts"
RUNNER = ROOT / "viral" / "runner.py"


def _ids(source: str, const: str) -> list[str]:
    """The `id:` values inside `export const <const> = [...]`."""
    block = re.search(rf"export const {const} = \[(.*?)\] as const;", source, re.S)
    assert block, f"{const} not found"
    return re.findall(r'id:\s*"([^"]+)"', block.group(1))


@pytest.fixture(scope="module")
def ts_lib() -> str:
    return TS_LIB.read_text(encoding="utf-8")


# ── the option vocabularies agree ────────────────────────────────────

def test_the_screen_offers_exactly_the_aspects_the_worker_supports(ts_lib):
    assert _ids(ts_lib, "ASPECTS") == list(clip.ASPECTS)


def test_the_screen_offers_exactly_the_qualities_the_worker_supports(ts_lib):
    assert _ids(ts_lib, "QUALITIES") == list(clip.QUALITIES)


def test_the_screen_offers_exactly_the_framing_modes_the_worker_supports(ts_lib):
    assert tuple(_ids(ts_lib, "FOCUS_MODES")) == clip.FOCUS_MODES


def test_the_screen_offers_exactly_the_caption_styles_the_worker_supports(ts_lib):
    assert tuple(_ids(ts_lib, "CAPTION_STYLES")) == clip.CAPTION_STYLES


def test_the_defaults_match_on_both_sides(ts_lib):
    def default(name: str) -> str:
        found = re.search(rf'export const {name} = "([^"]+)";', ts_lib)
        assert found, f"{name} not found"
        return found.group(1)

    assert default("DEFAULT_ASPECT") == clip.DEFAULT_ASPECT
    assert default("DEFAULT_QUALITY") == clip.DEFAULT_QUALITY
    assert default("DEFAULT_FOCUS") == clip.DEFAULT_FOCUS
    assert default("DEFAULT_CAPTION_STYLE") == clip.DEFAULT_CAPTION_STYLE


# ── the wire carries them ────────────────────────────────────────────

def test_the_screen_sends_every_option_it_shows():
    """A control that moves but is never transmitted is worse than no control."""
    ui = TS_UI.read_text(encoding="utf-8")
    assert "clipOptions: options" in ui, "the screen must send its options"
    for key in ("aspect", "focus", "captions", "caption_style", "quality", "speaker"):
        assert f'setOption("{key}"' in ui, f"{key} is never set by the screen"


def test_the_option_shape_is_complete(ts_lib):
    """Every field the worker reads must exist in the client's option type."""
    block = re.search(r"export type ClipOptions = \{(.*?)\};", ts_lib, re.S)
    assert block
    for key in ("aspect", "quality", "focus", "captions", "caption_style", "speaker"):
        assert key in block.group(1), f"{key} missing from ClipOptions"


def test_the_api_route_forwards_every_option():
    route = API_ROUTE.read_text(encoding="utf-8")
    assert "clip_options" in route, "the task payload must carry clip_options"
    for key in ("aspect", "quality", "focus", "captions", "caption_style", "speaker"):
        assert key in route, f"{key} is dropped by the API route"


def test_the_worker_reads_every_option():
    runner = RUNNER.read_text(encoding="utf-8")
    assert 'payload.get("clip_options")' in runner
    for key in ("aspect", "quality", "focus", "captions", "caption_style", "speaker"):
        assert f'options.get("{key}")' in runner, f"{key} never reaches the ClipSpec"


def test_an_absent_options_block_keeps_the_original_encode():
    """Existing callers must not be re-encoded differently by accident."""
    runner = RUNNER.read_text(encoding="utf-8")
    assert "build_ffmpeg_command(source_path, output, plan" in runner, (
        "the original single-clip path must still exist for callers that "
        "send no clip_options"
    )


# ── upload reaches the same place the worker downloads from ──────────

def test_the_upload_route_creates_an_asset_with_a_rights_source():
    upload = UPLOAD_ROUTE.read_text(encoding="utf-8")
    assert "rightsSource" in upload, "an uploaded asset must record its rights"
    assert "owned_or_permitted" in upload
    assert "ownsOrPermitted" in upload, "the confirmation must be required"


def test_the_upload_route_derives_the_path_from_the_session():
    """A commit naming somebody else's prefix must not attach their object."""
    upload = UPLOAD_ROUTE.read_text(encoding="utf-8")
    assert "vrf/uploads/${user.id}/" in upload
    assert "not yours" in upload


def test_the_uploaded_path_shape_matches_the_worker_download():
    """The worker downloads `storage_path`; the route stores a public URL."""
    upload = UPLOAD_ROUTE.read_text(encoding="utf-8")
    assert "publicUploadUrl" in upload
    assert "storagePath: url" in upload

    runner = RUNNER.read_text(encoding="utf-8")
    assert '_download(str(asset.get("storage_path")' in runner


# ── customer states ──────────────────────────────────────────────────

def test_the_screen_shows_five_states_and_no_others(ts_lib):
    block = re.search(r"export const CUSTOMER_STATES = \[(.*?)\] as const;", ts_lib, re.S)
    assert block
    states = re.findall(r'"([^"]+)"', block.group(1))
    assert states == ["Preparing", "Analyzing", "Creating clip", "Rendering", "Ready"]


def test_the_screen_never_renders_a_raw_task_error():
    ui = TS_UI.read_text(encoding="utf-8")
    assert "safeClipError(task.error)" in ui, (
        "a task row's error must pass through the customer-safe mapper"
    )
    assert "{task.error}" not in ui, "a raw worker error reached the screen"


def test_metadata_only_videos_cannot_be_selected():
    """A YouTube entry has no file and must never appear as an editable video."""
    ui = TS_UI.read_text(encoding="utf-8")
    assert "isProcessable" in ui
    assert "assets.filter(isProcessable)" in ui

    page = (ROOT / "web" / "app" / "dashboard" / "clipping" / "page.tsx").read_text(
        encoding="utf-8"
    )
    assert "storage_path" in page and "filter" in page, (
        "the page must filter to assets that actually have a file"
    )


# ── the advertised limits are the enforced limits ────────────────────

def test_the_screen_states_the_limit_the_server_enforces():
    ui = TS_UI.read_text(encoding="utf-8")
    uploads = (ROOT / "web" / "lib" / "uploads.ts").read_text(encoding="utf-8")
    assert "up to 2GB" in ui
    assert "2 * 1024 * 1024 * 1024" in uploads
    for fmt in ("MP4", "MOV", "WebM", "MKV"):
        assert fmt in ui, f"{fmt} is accepted but not advertised"
