# Video Factory — Web

Vercel front end for the Video Factory pipeline: enter a football topic, watch
generation progress, play and download the finished MP4.

## Why there is a separate worker

The pipeline cannot run on Vercel:

| Constraint | Vercel (Hobby) | Pipeline needs |
|---|---|---|
| Function duration | 60s max | 6–10 minutes per video |
| Bundle size | 250 MB unzipped | ~900 MB (OpenCV, NumPy, google-genai, Remotion) plus a Chromium download at render time |
| Binaries | none | FFmpeg, FFprobe, headless Chromium |

So Vercel hosts the UI and a thin JSON API, and a separate long-running
**worker** runs the existing `factory.py` unchanged.

```text
Browser ──► Vercel (Next.js UI + /api)
                     │  job state
                     ▼
              Vercel Blob  ◄── finished MP4 + thumbnail
                     ▲
                     │ polls /api/worker/claim, posts progress
              Worker (Python + FFmpeg + Remotion)
```

The worker **polls outbound only**, so it needs no public URL and works behind
NAT — on a workstation, a VM, or Cloud Run.

## Routes

| Route | Purpose |
|---|---|
| `POST /api/jobs` | Queue a generation. Rejects a second concurrent job with 409 |
| `GET /api/jobs` | Recent jobs, newest first |
| `GET /api/jobs/[id]` | One job's status |
| `POST /api/worker/claim` | Worker claims the next queued job (Bearer `WORKER_TOKEN`) |
| `POST /api/worker/update` | Worker posts progress and the final result (Bearer `WORKER_TOKEN`) |

## Environment variables

| Variable | Set by | Purpose |
|---|---|---|
| `BLOB_READ_WRITE_TOKEN` | Vercel, when the Blob store is connected | Job state + video storage |
| `WORKER_TOKEN` | you | Shared secret authenticating the worker. Must match `worker/.env` |
| `BLOB_BASE_URL` | optional | Overrides the Blob public host, normally derived from the token |

## Local development

```bash
cd web
npm install
vercel env pull .env.local
npm run dev
```

## Storage notes

Job state is a single `jobs/index.json` blob, read directly from the Blob CDN
rather than through the management API — `head()`/`list()` are much slower and
under load exceed the 30s function budget. The trade-off is that public blobs
carry a minimum 60s CDN TTL and the cache key ignores query strings, so a
reader can lag the writer by up to a minute. For a job that runs 6–10 minutes
that is a cosmetic delay in the progress bar; the terminal state still arrives.

The queue assumes a **single** worker instance. Blob has no compare-and-set, so
two concurrent workers could claim the same job. Run Cloud Run with
`--concurrency 1 --max-instances 1`, or one worker process.
