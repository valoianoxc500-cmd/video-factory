"""The URL-first Add-a-video flow, on the side that enforces it.

The UI now asks for a link and one confirmation. That is a genuine
simplification of the form, and it must not become a simplification of the
rules: a combined attestation is still an attestation, `discovered` is still
not a rights basis, and neither the browser nor a discovered result can talk
its way past either.

These read the shipped source rather than importing the TypeScript, the same
approach test_viral_web_parity.py already uses for the two sides of this
product.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WEB = REPO_ROOT / "web"

INGEST_TS = WEB / "lib" / "vrf-ingest.ts"
VRF_TS = WEB / "lib" / "vrf.ts"
ASSETS_ROUTE = WEB / "app" / "api" / "reels" / "assets" / "route.ts"
MY_VIDEOS = WEB / "components" / "reels" / "MyVideos.tsx"
DISCOVER = WEB / "components" / "reels" / "Discover.tsx"


def _read(path: Path) -> str:
    assert path.exists(), f"missing {path}"
    return path.read_text(encoding="utf-8")


# --- the attestation survives the redesign ---------------------------------

def test_the_combined_attestation_is_a_publishable_source():
    from viral.rights import PUBLISHABLE_SOURCES, Source

    assert Source.OWNED_OR_PERMITTED in PUBLISHABLE_SOURCES


def test_discovered_is_still_not_publishable():
    """The invariant the whole product rests on."""
    from viral.rights import PUBLISHABLE_SOURCES, Source

    assert Source.DISCOVERED not in PUBLISHABLE_SOURCES


def test_adding_without_the_confirmation_is_refused():
    source = _read(VRF_TS)
    block = source[source.index("async createFromUrl"):]
    block = block[: block.index("\n  }")]
    assert "if (!input.ownsOrPermitted)" in block, (
        "createFromUrl no longer requires the rights confirmation"
    )
    assert "ValidationError" in block


def test_the_asset_records_what_the_user_actually_confirmed():
    """Not own_recording -- the user was never asked to claim that."""
    source = _read(VRF_TS)
    block = source[source.index("async createFromUrl"):]
    assert 'rights_source: "owned_or_permitted"' in block


def test_ownership_comes_from_the_session_not_the_body():
    source = _read(VRF_TS)
    block = source[source.index("async createFromUrl"):]
    assert "user_id: userId" in block
    # A body-supplied user id would be the whole isolation model gone.
    assert "payload.userId" not in _read(ASSETS_ROUTE)
    assert "body.user_id" not in _read(ASSETS_ROUTE)


def test_connected_platforms_are_read_server_side():
    """Otherwise a client could assert a connection to unlock media fetching."""
    route = _read(ASSETS_ROUTE)
    assert 'from("vrf_accounts")' in route
    assert "connectedPlatforms" in route
    # Must not simply trust a list in the request body.
    assert re.search(r"payload\.connectedPlatforms", route) is None


# --- platform support ------------------------------------------------------

def test_only_platforms_with_an_official_route_are_accepted():
    source = _read(INGEST_TS)
    platforms = set(re.findall(r'^\s{4}platform: "(\w+)",', source, re.M))
    assert platforms == {"youtube", "instagram", "tiktok", "facebook"}


def test_youtube_is_declared_metadata_only():
    """There is no official endpoint that returns the media file."""
    source = _read(INGEST_TS)
    block = source[source.index("youtube: {"):]
    block = block[: block.index("},")]
    assert 'mediaAccess: "metadata_only"' in block


def test_owner_only_platforms_are_marked_as_such():
    """Instagram returns IG Media `media_url` and Facebook a Page video
    `source`, both for the owner's own posts."""
    source = _read(INGEST_TS)
    for platform in ("instagram", "facebook"):
        block = source[source.index(f"{platform}: {{"):]
        block = block[: block.index("},")]
        assert 'mediaAccess: "owner_connected_account"' in block, platform


def test_tiktok_is_declared_metadata_only():
    """The Display API's Video object carries no file field for anyone --
    owner included -- so promising an import there is promising nothing."""
    source = _read(INGEST_TS)
    block = source[source.index("tiktok: {"):]
    block = block[: block.index("},")]
    assert 'mediaAccess: "metadata_only"' in block
    assert "never the file" in block


def test_every_platform_explains_its_limitation():
    source = _read(INGEST_TS)
    limitations = re.findall(r"limitation:\s*\n?\s*[\"']", source)
    assert len(limitations) >= 4


def test_nothing_downloads_media_in_the_ingest_module():
    """This module decides what is permissible; it must not fetch."""
    source = _read(INGEST_TS)
    for forbidden in ("fetch(", "axios", "ytdl", "yt-dlp", "youtube-dl"):
        assert forbidden not in source, f"{forbidden} appears in vrf-ingest.ts"


def test_no_scraping_or_stream_ripping_tooling_anywhere_in_web():
    for path in WEB.rglob("*.ts"):
        if any(part in path.parts for part in ("node_modules", ".next", ".test-build")):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        for forbidden in ("yt-dlp", "ytdl-core", "youtube-dl", "stream-ripper"):
            assert forbidden not in text, f"{forbidden} referenced in {path.name}"


# --- the form asks for two things -----------------------------------------

def test_my_videos_asks_only_for_a_url_and_the_confirmation():
    source = _read(MY_VIDEOS)
    assert 'id="url"' in source
    assert "I own this content or have permission to reuse it." in source
    assert "Add & Process" in source

    # The multi-option rights form is gone from this component.
    assert "RIGHTS_SOURCES" not in source
    assert 'name="rightsSource"' not in source
    assert 'name="storagePath"' not in source


def test_my_videos_shows_the_required_fields_per_video():
    source = _read(MY_VIDEOS)
    for marker in (
        "thumbnail_url",
        "source_platform",
        "ingest_status",
        "original_viral_score",
        "new_version_probability",
        "publish_state",
    ):
        assert marker in source, f"My Videos does not show {marker}"


def test_the_probability_is_still_presented_as_an_estimate():
    source = _read(MY_VIDEOS)
    assert "not a guarantee" in source


# --- discover -> my videos -------------------------------------------------

def test_re_create_is_the_primary_action_on_a_result():
    source = _read(DISCOVER)
    assert "Re Create" in source
    # Primary, not one option among several.
    block = source[source.index('<div className="actions">'):]
    block = block[: block.index("</div>")]
    assert 'className="btn-primary"' in block


def test_re_create_requires_the_same_confirmation():
    source = _read(DISCOVER)
    block = source[source.index("function ClaimDialog"):]
    assert "I own this content or have permission to reuse it." in block
    assert "ownsOrPermitted: true" in block
    # It goes through the same route, so the same server-side gate applies.
    assert '"/api/reels/assets"' in block


def test_re_create_never_publishes():
    """Discovered content must not reach a platform by itself."""
    source = _read(DISCOVER)
    block = source[source.index("function ClaimDialog"):]
    assert "/api/reels/publish" not in block
    assert "publishNow" not in block


def test_re_create_asks_for_nothing_but_the_confirmation():
    """No variation settings, no instructions, no format choices."""
    source = _read(DISCOVER)
    block = source[source.index("function ClaimDialog"):]

    # One checkbox and the two dialog buttons. Any other input would be a
    # decision the user was asked to make before generating.
    inputs = re.findall(r"<input\b", block)
    assert len(inputs) == 1, f"the Re Create dialog has {len(inputs)} inputs"
    assert "<select" not in block
    assert "<textarea" not in block


def test_scores_are_not_inputs_anywhere_in_the_flow():
    """Viral Score and probability are results, not settings."""
    for path in (DISCOVER, MY_VIDEOS):
        source = _read(path)
        assert 'name="viralScore"' not in source
        assert 'name="viral_score"' not in source
        assert 'name="newVersionProbability"' not in source


def test_a_platform_that_cannot_release_media_says_so_immediately():
    """Rather than sending the user to a card that never progresses."""
    source = _read(DISCOVER)
    block = source[source.index("function ClaimDialog"):]
    assert 'ingest_status === "metadata_only"' in block
    assert "Cannot re-create this one" in block


def test_my_videos_reports_the_new_version_not_the_plumbing():
    source = _read(MY_VIDEOS)
    assert "New version ready" in source
    assert "Making the new version" in source


def test_discover_does_not_auto_add_anything():
    source = _read(DISCOVER)
    # The claim dialog is opened by a click, never from an effect.
    assert "setClaiming(video)" in source
    assert re.search(r"useEffect\([^)]*setClaiming\(", source) is None


# --- processing states -----------------------------------------------------

@pytest.mark.parametrize(
    "state",
    ["pending", "fetching", "imported", "metadata_only", "failed"],
)
def test_every_ingest_state_has_a_label(state):
    source = _read(MY_VIDEOS)
    block = source[source.index("const INGEST_LABEL"):]
    block = block[: block.index("};")]
    assert f"{state}:" in block, f"no label for ingest state {state}"


@pytest.mark.parametrize(
    "state", ["none", "queued", "scheduled", "published", "failed"]
)
def test_every_publish_state_has_a_label(state):
    source = _read(MY_VIDEOS)
    block = source[source.index("const PUBLISH_LABEL"):]
    block = block[: block.index("};")]
    assert f"{state}:" in block, f"no label for publish state {state}"


def test_a_metadata_only_asset_is_not_queued_for_processing():
    """Queuing work on a file that will never arrive leaves a stuck task."""
    route = _read(ASSETS_ROUTE)
    assert 'asset.ingest_status !== "metadata_only"' in route


def test_analysis_runs_even_without_the_media_file():
    """The original score comes from public metrics, not the file."""
    route = _read(ASSETS_ROUTE)
    analyse = route.index('"analyse"')
    # The media work is one task: importing the file and re-encoding it are
    # queued together, because the second needs what the first fetches.
    media = route.index('"ingest"')
    assert analyse < media, "analysis should be queued before the media work"
