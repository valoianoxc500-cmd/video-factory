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

# ── keyless upload credentials ───────────────────────────────────────

GCS_AUTH = ROOT / "web" / "lib" / "gcs-auth.ts"
UPLOADS_LIB = ROOT / "web" / "lib" / "uploads.ts"


def test_signing_does_not_require_a_service_account_key():
    """The org enforces iam.disableServiceAccountKeyCreation permanently."""
    uploads = UPLOADS_LIB.read_text(encoding="utf-8")
    # A key may still be read for local development, but it must not be the
    # only way to obtain a signer.
    assert "signerIdentity" in uploads
    assert '"federated"' in uploads
    assert "signBlobHex" in uploads, (
        "the V4 signature must be obtainable without a private key"
    )


def test_federation_is_preferred_over_a_local_key():
    """A stray dev key must never become the production signer."""
    uploads = UPLOADS_LIB.read_text(encoding="utf-8")
    resolver = re.search(
        r"export function signerIdentity\(\)[^{]*\{(.*?)\n\}", uploads, re.S
    )
    assert resolver, "signerIdentity not found"
    body = resolver.group(1)
    assert body.index("federationAvailable") < body.index("localKey"), (
        "the local key is checked before federation"
    )


def test_the_federation_exchange_uses_googles_supported_endpoints():
    auth = GCS_AUTH.read_text(encoding="utf-8")
    assert "https://sts.googleapis.com/v1/token" in auth
    assert "https://iamcredentials.googleapis.com/v1" in auth
    assert ":generateAccessToken" in auth
    assert ":signBlob" in auth
    assert "urn:ietf:params:oauth:grant-type:token-exchange" in auth


def test_the_oidc_assertion_comes_from_vercel():
    auth = GCS_AUTH.read_text(encoding="utf-8")
    assert "VERCEL_OIDC_TOKEN" in auth


def test_the_signature_is_converted_from_base64_to_hex():
    """signBlob answers base64; a V4 query string requires hex."""
    auth = GCS_AUTH.read_text(encoding="utf-8")
    assert 'Buffer.from(signed, "base64").toString("hex")' in auth


def test_uploads_still_go_straight_to_storage():
    """A 2GB file must never be proxied through a serverless function."""
    ui = TS_UI.read_text(encoding="utf-8")
    assert 'xhr.open("PUT", url, true)' in ui
    upload_route = UPLOAD_ROUTE.read_text(encoding="utf-8")
    assert "signedUploadUrl" in upload_route
    assert "await request.formData" not in upload_route, (
        "the route must not accept the file body itself"
    )


def test_federation_misconfiguration_is_diagnosable_but_not_customer_facing():
    auth = GCS_AUTH.read_text(encoding="utf-8")
    route = UPLOAD_ROUTE.read_text(encoding="utf-8")
    # Named variables in the server log...
    assert "GCP_WORKLOAD_IDENTITY_PROVIDER" in auth
    assert "GCP_SERVICE_ACCOUNT_EMAIL" in auth
    assert "console.error" in auth
    # ...and one safe sentence on the screen.
    assert "reportFederationFailure" in route
    assert "Uploads are not available right now." in route
    for leak in ("GCP_WORKLOAD_IDENTITY_PROVIDER", "sts.googleapis"):
        # The customer-facing string must not name infrastructure.
        assert f'{{ error: "{leak}' not in route


MEDIA_ROUTE = ROOT / "web" / "app" / "api" / "reels" / "clips" / "media" / "route.ts"


def test_reads_are_signed_by_the_same_keyless_path():
    uploads = UPLOADS_LIB.read_text(encoding="utf-8")
    assert "signedReadUrl" in uploads
    # One signer for both methods; a second copy is a second canonical
    # request to get subtly wrong.
    assert uploads.count("async function signV4") == 1
    assert 'signV4("GET"' in uploads
    assert 'signV4("PUT"' in uploads


def test_the_media_route_checks_ownership_before_signing():
    route = MEDIA_ROUTE.read_text(encoding="utf-8")
    assert "requireUser" in route
    assert "AssetRepository" in route
    assert ".get(assetId)" in route, (
        "the asset must be read through the caller's session, so RLS applies"
    )


def test_the_player_and_download_never_use_the_stored_object_url():
    """Using the stored URL directly requires a world-readable bucket."""
    ui = TS_UI.read_text(encoding="utf-8")
    assert "src={previewSrc}" in ui
    assert "const previewSrc = localUrl || sourceUrl;" in ui
    assert "/api/reels/clips/media?asset=" in ui
    for leak in ("src={selected?.storage_path", "href={selected.processed_path"):
        assert leak not in ui, f"a raw object URL reached the DOM: {leak}"


def test_clipping_has_no_production_dependency_on_a_json_key():
    """The org enforces iam.disableServiceAccountKeyCreation permanently."""
    # Every file in Clipping's end-to-end flow.
    flow = [
        UPLOADS_LIB,
        GCS_AUTH,
        UPLOAD_ROUTE,
        MEDIA_ROUTE,
        API_ROUTE,
        TS_UI,
        ROOT / "web" / "app" / "dashboard" / "clipping" / "page.tsx",
    ]
    for path in flow:
        source = path.read_text(encoding="utf-8")
        # media.ts is the library's read-signer and still uses a key; Clipping
        # must not reach it.
        assert "lib/media" not in source, f"{path.name} imports the key-based signer"

    uploads = UPLOADS_LIB.read_text(encoding="utf-8")
    # A key may still be *read* for local development, but from exactly one
    # place -- the development fallback. A second read site would be a second
    # way for production to end up depending on a key that cannot exist.
    assert uploads.count("process.env.GCS_SERVICE_ACCOUNT_JSON") == 1
    assert "function localKey()" in uploads
    assert "federationAvailable" in uploads

    # And the signer itself never reads the variable directly.
    signer = re.search(r"async function signV4\((.*?)\n\}", uploads, re.S)
    assert signer, "signV4 not found"
    assert "GCS_SERVICE_ACCOUNT_JSON" not in signer.group(1)


def test_a_read_path_cannot_escape_the_clipping_prefix():
    uploads = UPLOADS_LIB.read_text(encoding="utf-8")
    assert "assertClipReadPath" in uploads
    # Confined to vrf/, so the library's videos/ objects are unreachable.
    assert "vrf\\\\/uploads" in uploads or "vrf\\/uploads" in uploads
    assert "objectPathFromStored" in uploads
    assert 'parsed.hostname !== "storage.googleapis.com"' in uploads, (
        "a tampered row must not aim a signed URL at another host"
    )


def test_the_bucket_config_is_unchanged():
    uploads = UPLOADS_LIB.read_text(encoding="utf-8")
    assert "process.env.GCS_BUCKET" in uploads


def test_the_screen_states_the_limit_the_server_enforces():
    ui = TS_UI.read_text(encoding="utf-8")
    uploads = (ROOT / "web" / "lib" / "uploads.ts").read_text(encoding="utf-8")
    assert "up to 2GB" in ui
    assert "2 * 1024 * 1024 * 1024" in uploads
    for fmt in ("MP4", "MOV", "WebM", "MKV"):
        assert fmt in ui, f"{fmt} is accepted but not advertised"
