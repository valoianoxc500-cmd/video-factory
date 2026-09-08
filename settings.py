"""Global settings — paths, defaults, thresholds."""

import functools
import re
import subprocess
from pathlib import Path
from pydantic_settings import BaseSettings
from pydantic import Field


PROJECT_ROOT = Path(__file__).resolve().parent


def _resolve_main_repo_root() -> Path:
    """Find the main repo root, even when running from a git worktree.

    Bundled assets (music, SFX) live in the main repo and are gitignored,
    so they aren't copied into worktrees. This resolves to the main repo
    so assets are always found.
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if result.returncode == 0:
            git_common = Path(result.stdout.strip())
            if not git_common.is_absolute():
                git_common = (PROJECT_ROOT / git_common).resolve()
            main_root = git_common.parent
            if (main_root / "assets").exists():
                return main_root
    except Exception as e:
        import logging
        logging.getLogger("video_factory").debug(f"Worktree detection skipped: {e}")
    return PROJECT_ROOT


MAIN_REPO_ROOT = _resolve_main_repo_root()
ASSETS_DIR = MAIN_REPO_ROOT / "assets"

CHANNELS_DIR = PROJECT_ROOT / "config" / "channels"
DATA_DIR = MAIN_REPO_ROOT / "data"
LOGS_DIR = MAIN_REPO_ROOT / "logs"
WORKSPACE_DIR = MAIN_REPO_ROOT / "workspace"

# Ensure runtime dirs exist
for _d in (DATA_DIR, LOGS_DIR, WORKSPACE_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# Trust the OS certificate store for httpx and gRPC. Required on machines
# where TLS is inspected by endpoint security; a no-op everywhere else.
from core.netconfig import configure_outbound_tls  # noqa: E402

configure_outbound_tls(DATA_DIR)


class Settings(BaseSettings):
    """Environment-driven settings (override via env vars or .env)."""

    # Google AI
    google_project_id: str = Field(default="", alias="GOOGLE_PROJECT_ID")

    # Image sourcing
    serper_api_key: str = Field(default="", alias="SERPER_API_KEY")
    pexels_api_key: str = Field(default="", alias="PEXELS_API_KEY")
    pixabay_api_key: str = Field(default="", alias="PIXABAY_API_KEY")
    unsplash_access_key: str = Field(default="", alias="UNSPLASH_ACCESS_KEY")

    # AI gateway. `direct` is the existing behaviour: every model call goes
    # straight to Gemini through clients.py. `omniroute` sends chat completions
    # through an OpenAI-compatible gateway instead; image generation, research
    # grounding and TTS always stay direct.
    #
    # Every flag below defaults to the current behaviour, so an unconfigured
    # deployment is unchanged.
    ai_gateway_mode: str = Field(default="direct", alias="AI_GATEWAY_MODE")
    ai_gateway_base_url: str = Field(default="", alias="AI_GATEWAY_BASE_URL")
    ai_gateway_api_key: str = Field(default="", alias="AI_GATEWAY_API_KEY")
    ai_smart_routing_enabled: bool = Field(
        default=False, alias="AI_SMART_ROUTING_ENABLED")
    ai_cache_enabled: bool = Field(default=True, alias="AI_CACHE_ENABLED")
    ai_request_coalescing_enabled: bool = Field(
        default=True, alias="AI_REQUEST_COALESCING_ENABLED")

    # Audio. ElevenLabs is used for generated sound effects only -- narration
    # stays on Gemini TTS. Freesound supplies recorded ambience and SFX.
    elevenlabs_api_key: str = Field(default="", alias="ELEVENLABS_API_KEY")
    freesound_api_key: str = Field(default="", alias="FREESOUND_API_KEY")

    # Research providers. Wikimedia Commons needs no key, only a User-Agent.
    youtube_api_key: str = Field(default="", alias="YOUTUBE_API_KEY")
    # NEWSAPI_KEY is the spelling already in use; NEWSAPI_API_KEY is also
    # accepted by the provider itself.
    newsapi_api_key: str = Field(default="", alias="NEWSAPI_KEY")

    # Unsplash requires attribution links to carry the application's own utm
    # source, so the credit a video shows is traceable to this app.
    unsplash_app_name: str = Field(default="video_factory", alias="UNSPLASH_APP_NAME")

    # Gemini models
    gemini_primary_model: str = "gemini-3.1-pro-preview"
    gemini_review_model: str = "gemini-3-flash-preview"
    gemini_tts_model: str = "gemini-2.5-pro-tts"
    gemini_image_model: str = "gemini-3.1-flash-image-preview"
    # Model used for web-grounded news research. Flash is preferred:
    # grounding does the heavy lifting and it has far more quota room.
    gemini_research_model: str = "gemini-3-flash-preview"

    # Per-request timeout for Gemini calls, in milliseconds. TTS and image
    # generation are the slowest; anything beyond this is a stalled connection,
    # not a slow model, and is retried rather than waited on forever.
    gemini_request_timeout_ms: int = 300_000

    # Vertex AI
    google_cloud_location: str = Field(default="global", alias="GOOGLE_CLOUD_LOCATION")
    google_stt_location: str = Field(default="us-central1", alias="GOOGLE_STT_LOCATION")

    # Rendering
    ffmpeg_path: str = "ffmpeg"
    remotion_project_path: str = str(PROJECT_ROOT / "rendering" / "remotion")
    # Set false on hosts with no usable GPU (VMs, CI, remote desktops): the
    # Windows preflight then warns instead of aborting the render.
    remotion_require_gpu: bool = True
    # Final-encode quality for the delivered MP4. The pipeline previously
    # encoded at cq=18 (visually lossless, ~15 Mbps, ~112 MB per minute at
    # 1080x1920), which is far more than a social short needs and burned
    # through object-storage quota in nine videos. cq 26 with a 6 Mbps ceiling
    # is visually equivalent on a phone at roughly a third of the size.
    video_quality_cq: int = 26
    video_max_bitrate: str = "6M"
    video_buffer_size: str = "12M"
    audio_bitrate: str = "128k"
    # Browser tabs Remotion opens *within* one render. It defaults to roughly
    # the CPU count; each tab is a full Chromium rendering 1080x1920 frames, so
    # on a memory-constrained host the pool OOMs and the render dies in
    # makePage/setPropsAndEnv. 0 leaves Remotion's default in place.
    remotion_browser_concurrency: int = 0

    # How long one section may spend in Remotion before it is abandoned.
    #
    # Remotion drives headless Chrome, and a stalled GPU driver or a wedged
    # renderer process does not fail -- it simply never returns. Without a
    # ceiling the await never completes, the stage never ends, and the job sits
    # at "Rendering" until the machine is rebooted. A section of a 60s video
    # renders in roughly one to three minutes, so ten is generous enough to be
    # a hang detector rather than a limit on normal work.
    remotion_render_timeout_seconds: float = 600.0

    # A hung render is usually transient -- a Chrome instance that failed to
    # start, a GPU context lost under load. Retrying the section costs minutes;
    # losing the run costs everything before it.
    remotion_render_attempts: int = 3

    model_config = {"env_file": str(PROJECT_ROOT / ".env"), "extra": "ignore"}


settings = Settings()


@functools.lru_cache(maxsize=1)
def get_remotion_compositions() -> tuple[str, ...]:
    """Read available Remotion composition IDs from Root.tsx (single source of truth)."""
    root_tsx = Path(settings.remotion_project_path) / "src" / "Root.tsx"
    if not root_tsx.exists():
        return ()
    return tuple(re.findall(r'id="(\w+)"', root_tsx.read_text(encoding="utf-8")))
