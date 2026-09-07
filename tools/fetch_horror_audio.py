"""Fetch the Horror Stories music beds and SFX from Wikimedia Commons.

Uses the same licence-filtered path as the footage discovery module: results
are kept only when Commons publishes a reuse licence on the allowlist, and
bytes are only ever fetched from an allowlisted host. Nothing here scrapes a
search engine or a streaming site.

Each asset is transcoded to MP3 at the stem name the channel's `music_pool`
and `sfx_pool` reference, and loudness-normalised so a bed cannot arrive
louder than the mix expects. Provenance -- URL, licence, attribution -- is
written to `assets/audio_provenance.json`.

Usage:
    python tools/fetch_horror_audio.py [--force] [--dry-run]

Attribution: CC BY and CC BY-SA tracks REQUIRE crediting the artist wherever
the video is published. The provenance file lists exactly what to credit.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from core.footage_discovery import (  # noqa: E402
    AUDIO_EXTENSIONS,
    is_downloadable_url,
    is_reusable_licence,
)
from settings import ASSETS_DIR, settings  # noqa: E402

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
UA = "VideoFactory/1.0 (horror audio assets; contact via repo)"

# Beds are long enough to cover a short video without looping audibly, and
# each is a distinct mood so the scripter can choose by intensity.
MUSIC: list[dict] = [
    {
        "stem": "horror_dread_low",
        "mood": "slow, low, oppressive dread for openings and quiet menace",
        "search": "Alex-Productions Deep Dark Ambient Background music",
        "target_lufs": -26.0,
    },
    {
        "stem": "horror_tension_pulse",
        "mood": "restless investigative tension for the rising middle",
        "search": "PeriTune Investigation2 Suspense Royalty Free Music",
        "target_lufs": -26.0,
    },
    {
        "stem": "horror_climax_dark",
        "mood": "dense, dark intensity for the climax and reveal",
        "search": "Dreamstate Logic Zero Point space ambient dark ambient",
        "target_lufs": -26.0,
    },
]

# Short, punctuating one-shots. Preferring public-domain/CC0 here keeps the
# attribution burden on the three music beds only.
SFX: list[dict] = [
    {
        "stem": "horror_door_creak",
        "search": "Door handle creaking",
        "max_seconds": 8.0,
        "target_lufs": -20.0,
    },
    {
        "stem": "horror_heartbeat",
        "search": "Heartbeat mitral valve 150 bpm",
        "max_seconds": 12.0,
        "target_lufs": -20.0,
    },
    {
        "stem": "horror_jarring_hit",
        "search": "Dreadful jarring of dis",
        "max_seconds": 10.0,
        "target_lufs": -20.0,
    },
]


def search_commons_audio(client: httpx.Client, query: str, limit: int = 10) -> list[dict]:
    """Reusable, downloadable audio results for `query`.

    Commons answers 429 to bursts, so requests are spaced and backed off
    rather than retried tightly -- this is a shared volunteer-run API.
    """
    params = {
        "action": "query", "format": "json",
        "generator": "search",
        "gsrsearch": f"{query} filetype:audio",
        "gsrnamespace": "6", "gsrlimit": str(limit),
        "prop": "imageinfo",
        "iiprop": "url|extmetadata|size|mime",
    }
    resp = None
    for attempt in range(1, 5):
        time.sleep(1.5 if attempt == 1 else 4.0 * attempt)
        resp = client.get(COMMONS_API, params=params, timeout=45)
        if resp.status_code != 429:
            break
        print(f"      rate limited, backing off (attempt {attempt}/4)")
    assert resp is not None
    resp.raise_for_status()
    pages = (resp.json().get("query") or {}).get("pages") or {}

    out: list[dict] = []
    for page in pages.values():
        info = (page.get("imageinfo") or [{}])[0]
        url = info.get("url") or ""
        meta = info.get("extmetadata") or {}
        licence = str((meta.get("LicenseShortName") or {}).get("value") or "")
        artist = str((meta.get("Artist") or {}).get("value") or "")
        artist = _strip_markup(artist)

        if not is_reusable_licence(licence):
            continue
        if not is_downloadable_url(url, AUDIO_EXTENSIONS):
            continue
        out.append({
            "title": str(page.get("title") or "")[5:],
            "url": url,
            "licence": licence,
            "attribution": artist,
            "duration": float(info.get("duration") or 0.0),
            "bytes": int(info.get("size") or 0),
            "source_page": str(page.get("title") or "").replace(" ", "_"),
        })
    return out


def _strip_markup(text: str) -> str:
    import re
    return re.sub(r"<[^>]+>", "", text).strip()


def _transcode(
    src: Path,
    dest: Path,
    *,
    target_lufs: float,
    max_seconds: float | None,
) -> None:
    """Transcode to MP3, loudness-normalised so it sits under narration."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    filters = [f"loudnorm=I={target_lufs}:TP=-2.0:LRA=11"]
    cmd = [settings.ffmpeg_path, "-y", "-i", str(src)]
    if max_seconds:
        cmd += ["-t", f"{max_seconds:.2f}"]
    cmd += [
        "-af", ",".join(filters),
        "-ac", "2", "-ar", "44100", "-b:a", "160k",
        str(dest),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True,
                            encoding="utf-8", errors="replace")
    if result.returncode != 0 or not dest.exists():
        raise RuntimeError(f"ffmpeg failed for {dest.name}:\n{result.stderr[-600:]}")


def fetch(spec: dict, out_dir: Path, client: httpx.Client, *, force: bool,
          dry_run: bool) -> dict | None:
    dest = out_dir / f"{spec['stem']}.mp3"
    if dest.exists() and not force:
        print(f"  exists, skipping: {dest.name}")
        return None

    results = search_commons_audio(client, spec["search"])
    if not results:
        print(f"  NO reusable result for {spec['stem']} ({spec['search']!r})")
        return None
    pick = results[0]
    print(f"  {spec['stem']}: {pick['title'][:56]}")
    print(f"      licence={pick['licence']}  {pick['duration']:.0f}s  "
          f"{pick['bytes']/1e6:.1f}MB")
    if dry_run:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f"_tmp_{spec['stem']}{Path(pick['url'].split('?')[0]).suffix}"
    with client.stream("GET", pick["url"], timeout=180) as r:
        r.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in r.iter_bytes(1 << 20):
                fh.write(chunk)
    try:
        _transcode(
            tmp, dest,
            target_lufs=spec["target_lufs"],
            max_seconds=spec.get("max_seconds"),
        )
    finally:
        tmp.unlink(missing_ok=True)

    print(f"      -> {dest.name} ({dest.stat().st_size/1e6:.2f} MB)")
    return {
        "stem": spec["stem"],
        "file": str(dest.relative_to(ASSETS_DIR.parent)),
        "kind": "music" if "mood" in spec else "sfx",
        "mood": spec.get("mood", ""),
        "platform": "wikimedia_commons",
        "url": pick["url"],
        "source_page": f"https://commons.wikimedia.org/wiki/{pick['source_page']}",
        "licence": pick["licence"],
        "attribution": pick["attribution"],
        "attribution_required": pick["licence"].lower().startswith("cc by"),
        "title": pick["title"],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-download existing assets")
    ap.add_argument("--dry-run", action="store_true", help="search only, download nothing")
    args = ap.parse_args()

    records: list[dict] = []
    with httpx.Client(follow_redirects=True, headers={"User-Agent": UA}) as client:
        print("music beds:")
        for spec in MUSIC:
            rec = fetch(spec, ASSETS_DIR / "music", client,
                        force=args.force, dry_run=args.dry_run)
            if rec:
                records.append(rec)
        print("sfx:")
        for spec in SFX:
            rec = fetch(spec, ASSETS_DIR / "sfx" / "horror", client,
                        force=args.force, dry_run=args.dry_run)
            if rec:
                records.append(rec)

    if args.dry_run or not records:
        return

    prov = ASSETS_DIR / "audio_provenance.json"
    existing = []
    if prov.exists():
        try:
            existing = json.loads(prov.read_text(encoding="utf-8")).get("assets", [])
        except Exception:
            existing = []
    by_stem = {a.get("stem"): a for a in existing}
    for rec in records:
        by_stem[rec["stem"]] = rec
    prov.write_text(
        json.dumps({"assets": list(by_stem.values())}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nprovenance -> {prov}")
    needs = [r for r in by_stem.values() if r.get("attribution_required")]
    if needs:
        print("\nATTRIBUTION REQUIRED when publishing:")
        for r in needs:
            print(f"  {r['stem']}: {r['title']} — {r['attribution']} ({r['licence']})")


if __name__ == "__main__":
    main()
