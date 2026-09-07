# Production Deployment

Deployment guide for Video Factory. For what the pipeline does and how the
stages fit together, see [README.md](README.md).

---

## 1. Runtime requirements

| Component | Required | Verified on | Notes |
|---|---|---|---|
| Python | 3.10+ (**3.11+ recommended**) | 3.10.10 | `google-api-core` drops 3.10 support after 2026-10-04 |
| Node.js | 18+ | 24.11.1 | Runs the Remotion renderer |
| npm | 9+ | 11.6.2 | Ships with Node |
| FFmpeg | 6.0+ with `ffprobe` | 2026-01-12 full build | Both binaries must be on `PATH` |
| Remotion | 4.0.443 | pinned in `package.json` | Installed by `npm install` |

### FFmpeg

`ffmpeg` **and** `ffprobe` must both resolve on `PATH` — the pipeline shells out
to `ffprobe` for duration probing and output validation.

```bash
ffmpeg -version
ffprobe -version
```

To use a build that is not on `PATH`, set `FFMPEG_PATH` to the `ffmpeg`
executable. `ffprobe` is still resolved from `PATH`.

### Fonts (Arabic / RTL channels)

Thumbnail headlines for right-to-left languages are composited with Pillow, so
the host needs a font that actually contains Arabic glyphs — **Arial**,
**Tahoma**, or **Noto Naskh Arabic**. Faces without Arabic coverage (Segoe UI
Black, for example) are detected and skipped automatically. On a minimal Linux
image:

```bash
apt-get install -y fonts-noto-core fonts-noto-naskh-arabic
```

---

## 2. Install

```bash
git clone <your-repo-url> video-factory
cd video-factory
```

### Python dependencies

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

`requirements.txt` pulls in:

| Package | Purpose |
|---|---|
| `google-genai`, `google-cloud-speech`, `google-cloud-bigquery` | Gemini, Speech-to-Text, cost reconciliation |
| `Pillow`, `opencv-python`, `numpy` | Image processing, face-aware crop, audio buffers |
| `arabic-reshaper`, `python-bidi` | Arabic shaping and bidi for thumbnail headlines |
| `httpx`, `aiofiles` | Async image sourcing and downloads |
| `truststore` | Makes Python trust the OS certificate store (see §6) |
| `click`, `rich` | CLI and console output |
| `pydantic`, `pydantic-settings` | Config schema and env loading |

To run the test suite, also install:

```bash
pip install pytest pytest-asyncio
```

### Node dependencies

```bash
cd rendering/remotion
npm install
cd ../..
```

Remotion downloads a Chromium build on first render; allow outbound HTTPS or
pre-warm it during image build.

### Bundled audio assets

Background music and transition SFX are read from `assets/music/*.mp3` and
`assets/sfx/transitions/*.mp3`, which are gitignored. Generate synthesized
placeholders so a fresh checkout produces a complete mix:

```bash
python tools/make_default_audio_assets.py
```

Replace them with licensed tracks when you have them, keeping the stem names
listed in each channel's `video.music_pool`.

---

## 3. Google Cloud

### Required APIs

Enable both on the target project:

| API | Service | Used for |
|---|---|---|
| `aiplatform.googleapis.com` | Vertex AI | Gemini text, vision review, TTS, thumbnail image generation |
| `speech.googleapis.com` | Speech-to-Text V2 | Word-level caption timestamps |

```bash
gcloud services enable aiplatform.googleapis.com speech.googleapis.com \
  --project "$GOOGLE_PROJECT_ID"
```

Optional: `bigquery.googleapis.com` if you use `tools/reconcile_gcp_costs.py`
to compare estimated against billed spend.

### Authentication

The pipeline uses **Application Default Credentials**. It never reads a key
file path from config, and no credentials belong in the repo.

**Workstation / VM with a human operator:**

```bash
gcloud auth application-default login
gcloud config set project your-gcp-project-id
```

**Server, container, or CI** — attach a service account instead:

- On GCP (GCE, Cloud Run, GKE): attach the service account to the workload;
  ADC is resolved from the metadata server with no key file.
- Off GCP: use Workload Identity Federation, or as a last resort mount a
  service-account JSON and point `GOOGLE_APPLICATION_CREDENTIALS` at it.
  Keep that file outside the repo — `.gitignore` blocks the common names, but
  the safest location is a mounted secret volume.

Minimum IAM roles:

| Role | Why |
|---|---|
| `roles/aiplatform.user` | Gemini generate/vision/TTS/image calls |
| `roles/speech.client` | Speech-to-Text V2 recognition |
| `roles/bigquery.jobUser` + `roles/bigquery.dataViewer` | Only for cost reconciliation |

### Model availability

Model access varies per project. Before first run, confirm the configured
models resolve — a missing model returns `404 NOT_FOUND` and a quota-starved
one returns `429 RESOURCE_EXHAUSTED`. Override any of them via the environment
variables in §4 rather than editing code.

Speech-to-Text region matters: **`GOOGLE_STT_LOCATION` must be a regional
endpoint such as `us-central1`.** The `global` endpoint does not serve the
`chirp_2` model that Arabic recognition requires.

---

## 4. Environment variables

Copy the template and fill it in. `.env` is gitignored and must never be
committed.

```bash
cp .env.example .env
```

### Required

| Variable | Description |
|---|---|
| `GOOGLE_PROJECT_ID` | Vertex AI project id |
| `SERPER_API_KEY` | Serper.dev key — web image search (the source of all real photos) |

### Recommended

| Variable | Default | Description |
|---|---|---|
| `GOOGLE_CLOUD_LOCATION` | `global` | Vertex AI region |
| `GOOGLE_STT_LOCATION` | `us-central1` | Speech-to-Text region — must be regional, not `global` |

### Optional

| Variable | Default | Description |
|---|---|---|
| `PEXELS_API_KEY` | *(unset)* | Stock photos / B-roll. Must be pure ASCII; a key with stray characters is rejected with a warning and Pexels is skipped |
| `GEMINI_PRIMARY_MODEL` | `gemini-3.1-pro-preview` | Planning and script generation |
| `GEMINI_REVIEW_MODEL` | `gemini-3-flash-preview` | Vision review gates |
| `GEMINI_TTS_MODEL` | `gemini-2.5-pro-tts` | Narration TTS |
| `GEMINI_IMAGE_MODEL` | `gemini-3.1-flash-image-preview` | Thumbnail artwork |
| `FFMPEG_PATH` | `ffmpeg` | Path to the FFmpeg binary |
| `REMOTION_PROJECT_PATH` | `rendering/remotion` | Remotion project location |
| `REMOTION_REQUIRE_GPU` | `true` | Set `false` on hosts with no hardware-accelerated Chromium (VMs, CI, remote desktops). The preflight then warns instead of aborting |
| `GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` | *(auto)* | CA bundle for gRPC. Auto-derived from the Windows store; set explicitly on Linux behind a TLS-inspecting proxy |

Model ids and regions are configuration, not secrets. Only `SERPER_API_KEY` and
`PEXELS_API_KEY` are credentials.

---

## 5. Production startup command

```bash
python factory.py --channel football_news --allow-review-failures image_review,final_review
```

`image_review` and `final_review` have no regeneration path — they run once and
report. Listing them lets an advisory opinion (a watermark, a debatable photo
choice) be recorded in `checkpoint.json` instead of discarding a finished
video. Drop the flag to make those gates blocking.

Useful variants:

```bash
# Resume a run that stopped partway
python factory.py --channel football_news --workspace workspace/<run-dir> --stage render_sections..final_review

# Run one stage or a range
python factory.py --channel football_news --stage image_source..audio_source

# Zero-cost replay from recorded fixtures
python factory.py --channel football_news --fixtures replay
```

On Windows, set `PYTHONUTF8=1` so Arabic titles and paths survive the console
codepage.

### Output

Each run writes `workspace/{channel}_{timestamp}_{uuid}/`:

```text
├── <YYYY-MM-DD_HHMMSS_title-slug>.mp4   # final video, 1080x1920
├── thumbnail.png                        # 1280x720
├── script.json                          # narration, slots, word timestamps
├── checkpoint.json                      # stage state + review log
├── pipeline.html                        # AI trace viewer
└── reports/                             # cost estimate, per-stage reports
```

### Health check

```bash
python factory.py --help          # config loads, imports resolve
python -m pytest -q               # full suite
```

---

## 6. Networking

Outbound HTTPS is required to `*.googleapis.com`, `google.serper.dev`,
`api.pexels.com`, and arbitrary image-hosting domains returned by search.

If the host runs TLS-inspecting endpoint security, Python and gRPC will both
fail certificate verification and **every** provider call breaks.
`core/netconfig.py` handles this automatically: it injects `truststore` so
`httpx` uses the OS certificate store, and on Windows exports the system root
store to `data/system_roots.pem` for gRPC. On Linux behind such a proxy, point
`GRPC_DEFAULT_SSL_ROOTS_FILE_PATH` at a bundle containing the inspection root.

---

## 7. Pre-deployment checklist

- [ ] `ffmpeg -version` and `ffprobe -version` both succeed
- [ ] `node -v` reports 18 or newer
- [ ] `pip install -r requirements.txt` completed
- [ ] `npm install` completed in `rendering/remotion`
- [ ] `python tools/make_default_audio_assets.py` run, or licensed tracks placed in `assets/music/`
- [ ] A font with Arabic glyphs is installed (RTL channels only)
- [ ] `aiplatform.googleapis.com` and `speech.googleapis.com` enabled
- [ ] ADC configured and the service account holds `aiplatform.user` + `speech.client`
- [ ] `.env` created from `.env.example`, with `GOOGLE_PROJECT_ID` and `SERPER_API_KEY` set
- [ ] `GOOGLE_STT_LOCATION` is a regional endpoint, not `global`
- [ ] `.env` is **not** tracked: `git ls-files --error-unmatch .env` errors out
- [ ] `python -m pytest -q` passes
- [ ] One full run completes: the startup command above exits 0

---

## 8. Web app: Google sign-in and private media

Both items below need configuration in external consoles. The code is in place
and tested; neither feature can work until these values exist.

### 8.1 Google OAuth

Nothing in this repository holds a Google credential. Supabase stores the
client secret and performs the token exchange; the app only ever sees the
resulting session.

**A. Google Cloud Console** — project `project-c7d39787-bbc0-4b81-8ba`

1. *APIs & Services → OAuth consent screen*: configure it (External is fine),
   add your own address as a test user while it is unpublished.
2. *APIs & Services → Credentials → Create credentials → OAuth client ID*
   - Application type: **Web application**
   - **Authorised JavaScript origins**:
     - `http://localhost:3300`
     - `https://video-factory-omega.vercel.app`
     - `https://video-factory-valoianoxc500-7295s-projects.vercel.app`
   - **Authorised redirect URI** — exactly one, and it points at Supabase, not
     at this app:
     - `https://fiichyrbvhvijcoxdewk.supabase.co/auth/v1/callback`
3. Copy the generated **Client ID** and **Client secret**.

**B. Supabase Dashboard** — project `fiichyrbvhvijcoxdewk`

1. *Authentication → Providers → Google*: enable it, paste the Client ID and
   Client secret from step A3, save.
2. *Authentication → URL Configuration*:
   - **Site URL**: `https://video-factory-omega.vercel.app`
   - **Redirect URLs** (one per line; the wildcard covers preview deploys):
     - `http://localhost:3300/auth/callback`
     - `https://video-factory-omega.vercel.app/auth/callback`
     - `https://video-factory-*-valoianoxc500-7295s-projects.vercel.app/auth/callback`

The app builds its callback from `window.location.origin`, so no per-
environment variable is needed — local, preview and production all work from
the same build once those URLs are allow-listed.

### 8.2 Private media and V4 signed URLs

The bucket `video-factory-media-c7d39787` currently grants
`roles/storage.objectViewer` to `allUsers`, which is what makes media load
today. Signing is implemented and its signature is verified cryptographically
in the test suite, but no key exists to sign with: application default
credentials on this machine are of type `authorized_user`, which cannot sign.

A service account already exists:
`video-factory@project-c7d39787-bbc0-4b81-8ba.iam.gserviceaccount.com`

1. *IAM & Admin → Service Accounts* → that account → **Keys → Add key →
   Create new key → JSON**. Download it. Do not commit it.
2. *Cloud Storage → Buckets →* `video-factory-media-c7d39787` *→ Permissions
   → Grant access*:
   - Principal: the service-account email above
   - Role: **Storage Object Viewer** (`roles/storage.objectViewer`)

   A signed URL carries the signer's own permission, so the signer must be able
   to read the objects. Uniform bucket-level access is enabled and locked on
   this bucket, so this must be an IAM grant — object ACLs will not work.
3. Set the whole JSON file, as one line, as `GCS_SERVICE_ACCOUNT_JSON`:
   - Vercel: *Settings → Environment Variables*, all three environments,
     marked **Sensitive**.
   - Local: add it to `web/.env.local` (already git-ignored).

Media responses report which mode they used. Until the key is set the API
returns `{"mode":"public"}` and a plain bucket URL; once it is set the same
endpoint returns `{"mode":"signed"}` with a 15-minute URL, with no code
change.

### 8.3 Only after signed URLs are confirmed working

Make the bucket private by removing public read:

- *Cloud Storage → Buckets →* `video-factory-media-c7d39787` *→ Permissions*
- Delete the `allUsers` entry for **Storage Object Viewer**

Confirm a library thumbnail and a video still play first. Removing that binding
while signing is unconfigured takes the media offline.
---

## 9. Viral Reels Finder

A second product in the same deployment. It shares authentication, RLS and the
worker-token pattern, and adds its own tables (`vrf_*`), its own worker process
and its own dashboard section under `/dashboard/reels`.

### 9.1 What each platform can actually do

Only YouTube has a public API for open trend discovery. TikTok's Research API is
granted case by case, Instagram and Facebook expose only accounts you manage,
and Snapchat publishes no discovery API at all. Publishing is available on
YouTube, Instagram, Facebook and TikTok; Snapchat has no server-side publishing
API, so selecting it is refused rather than queued.

The application reports this in the interface rather than failing quietly. Do
not expect discovery results from anything but YouTube.

### 9.2 Environment variables

Required for the worker:

- `WORKER_API_BASE` - the deployment base URL, e.g. `https://your-app.vercel.app`
- `WORKER_TOKEN` - the existing shared worker token
- `VRF_TOKEN_KEY` - base64 of 32 random bytes; encrypts stored OAuth tokens

Generate the key once and set it on both the web deployment and the worker:

```
python -c "from viral.accounts import generate_token_key; print(generate_token_key())"
```

Without it, connecting an account is refused. There is deliberately no path
that stores a token unencrypted.

Discovery (optional; without it discovery reports why it cannot search):

- `YOUTUBE_API_KEY` - a YouTube Data API v3 key

Publishing, per platform you want to enable:

- `YOUTUBE_OAUTH_CLIENT_ID` / `YOUTUBE_OAUTH_CLIENT_SECRET`
- `META_OAUTH_CLIENT_ID` / `META_OAUTH_CLIENT_SECRET` (Instagram and Facebook)
- `TIKTOK_CLIENT_KEY` / `TIKTOK_CLIENT_SECRET`
- `TIKTOK_DIRECT_POST_APPROVED=true` only once TikTok has audited the app;
  until then posts go to the user's drafts and the interface says so

OAuth redirect URI to register with every provider:

```
https://your-app.example/api/reels/accounts/callback
```

### 9.3 Running the worker

Alongside the video worker, not instead of it:

```
python vrf_worker.py            # poll continuously
python vrf_worker.py --once     # one pass, for a smoke test
```

It polls `POST /api/worker/vrf` for two queues: tasks (discovery, analysis,
video processing) and due publish jobs, including scheduled posts whose time
has arrived.

### 9.4 What it will not do

The processing stage re-encodes, reframes to 9:16 around the subject and
normalises loudness. It does not mirror, pitch-shift, alter speed, overlay
noise or strip metadata: those steps exist to defeat copyright and
duplicate-content detection, and `viral.rights.is_prohibited_technique` refuses
any request for them.

Publishing requires a rights attestation on the user's own video. Videos found
through Discover carry the `discovered` basis, which is never publishable -
they are reference material for analysis only.

---

## 10. Media and research providers

Six sources behind one interface (`core.providers`). They run in parallel, each
with its own retry and rate-limit handling, and one being unconfigured or
failing costs only its own results.

    from core.providers import search_media, search_research, availability_report

### 10.1 What each one is for

| Provider | Returns | Licence | Key |
|---|---|---|---|
| Pexels | photos, video | Pexels License, no credit required | `PEXELS_API_KEY` |
| Pixabay | photos, video | Pixabay Content License, no credit required | `PIXABAY_API_KEY` |
| Unsplash | photos | Unsplash License, **credit required** | `UNSPLASH_ACCESS_KEY` |
| Wikimedia Commons | photos, video, audio | per file, filtered to reusable | none |
| YouTube | research only | metadata; never the file | `YOUTUBE_API_KEY` |
| NewsAPI | research only | headline, summary, link | `NEWSAPI_KEY` |

`availability_report()` prints what is configured and what each missing key
would enable. A provider without its key says so; it does not silently return
nothing.

### 10.2 Obligations the code honours, not just records

Unsplash's API guidelines require two things beyond the licence text, and both
are implemented rather than noted:

- attribution naming the photographer and Unsplash, linking to their profile
  with this application's `utm_source` (set `UNSPLASH_APP_NAME`)
- a request to the photo's download endpoint whenever a photo is actually
  used, which `report_use` performs once a photo ships

Wikimedia Commons files are filtered through the same `is_reusable_licence`
gate the footage path uses, so non-commercial and no-derivatives material is
rejected rather than downloaded.

Pixabay forbids hot-linking, so its URLs are downloaded and stored, never
embedded.

### 10.3 Research is not media

YouTube's Data API never returns a video file, and a news article's text
belongs to its publisher. Both produce `ResearchItem`, which carries a
headline, the provider's own summary and a link -- and no media URL that could
be mistaken for something to republish. Every record is tagged
`usage: research_only`.

### 10.4 The open-library tier in image sourcing

`image_sourcing.open_library_fallback` asks Pixabay, Unsplash and Commons for
a beat the configured source could not fill, before the pipeline falls back to
a generated image. Three more catalogues of real photographs is a better
answer for a real subject than an invented one.

Off by default, so no channel gains three upstreams by upgrading and no test
run reaches for the network. Horror Stories and Football News opt in. Pexels
keeps its own measured path -- the query ladder, relevance ranking and
cross-beat de-duplication are unchanged, and this tier only runs after it.
