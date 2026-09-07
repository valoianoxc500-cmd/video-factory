"""Synthesize the bundled music beds and transition SFX with FFmpeg.

`assets/music/` and `assets/sfx/transitions/` are gitignored, so a fresh
checkout has no audio to mix. This generates royalty-free-by-construction
placeholders so `audio_source` and `assemble` produce a complete mix without
any manual file copying.

Replace the generated files with licensed tracks whenever you have them --
keep the stem names listed in the channel's `video.music_pool`.

Usage:
    python tools/make_default_audio_assets.py [--force]
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from settings import ASSETS_DIR, settings  # noqa: E402

MUSIC_SECONDS = 180

# Sparse, low-register beds that sit under narration without masking it.
MUSIC_BEDS: dict[str, str] = {
    # Calm: two soft sine partials with a slow tremolo.
    "news_bed_calm": (
        "sine=frequency=110:duration={d},"
        "tremolo=f=0.25:d=0.35,"
        "aeval=val(0)*0.5|val(0)*0.5,"
        "highpass=f=60,lowpass=f=1200,"
        "volume=0.5"
    ),
    # Drive: a slightly brighter bed with a faster pulse for news openers.
    "news_bed_drive": (
        "sine=frequency=146.83:duration={d},"
        "tremolo=f=1.5:d=0.5,"
        "aeval=val(0)*0.5|val(0)*0.5,"
        "highpass=f=70,lowpass=f=2000,"
        "volume=0.45"
    ),
}

# Short filtered-noise whooshes for section boundaries.
SFX: dict[str, str] = {
    "whoosh_soft": (
        "anoisesrc=d=0.8:c=pink:a=0.35,"
        "highpass=f=300,lowpass=f=4000,"
        "afade=t=in:st=0:d=0.35,afade=t=out:st=0.35:d=0.45,"
        "aeval=val(0)|val(0)"
    ),
    "whoosh_rise": (
        "anoisesrc=d=0.7:c=white:a=0.3,"
        "highpass=f=600,lowpass=f=6000,"
        "afade=t=in:st=0:d=0.5,afade=t=out:st=0.5:d=0.2,"
        "aeval=val(0)|val(0)"
    ),
}


def _render(filter_complex: str, destination: Path, force: bool) -> bool:
    if destination.exists() and not force:
        print(f"exists, skipping: {destination}")
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_path, "-y",
        "-filter_complex", filter_complex,
        "-ar", "44100", "-b:a", "128k",
        str(destination),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed for {destination.name}:\n{result.stderr[-800:]}"
        )
    print(f"wrote {destination} ({destination.stat().st_size} bytes)")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="overwrite existing asset files")
    args = parser.parse_args()

    for stem, spec in MUSIC_BEDS.items():
        _render(
            spec.format(d=MUSIC_SECONDS),
            ASSETS_DIR / "music" / f"{stem}.mp3",
            args.force,
        )
    for stem, spec in SFX.items():
        _render(
            spec,
            ASSETS_DIR / "sfx" / "transitions" / f"{stem}.mp3",
            args.force,
        )


if __name__ == "__main__":
    main()
