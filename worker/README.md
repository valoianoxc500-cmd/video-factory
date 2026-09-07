# Video Factory — Worker

Runs the existing pipeline (`factory.py`) for jobs queued by the Vercel app,
then uploads the finished MP4 and thumbnail to object storage.

Storage is pluggable (`worker/storage.py`), selected by
`MEDIA_STORAGE_PROVIDER`, so every channel — football, horror, history — shares
one backend. The default is Google Cloud Storage, which uploads resumably in
8 MiB chunks and has no per-object size ceiling. It authenticates with the same
Application Default Credentials the pipeline already uses for Vertex AI and
STT, so the worker needs no extra secret. The bucket must grant
`roles/storage.objectViewer` to `allUsers` for browser playback.

The `supabase` provider is kept for rollback only: its free tier rejects any
object over 50 MB on both the standard and the resumable endpoint, and finished
videos run 97–121 MB.

Nothing in `core/`, `factory.py`, or the channel configs is bypassed: the
worker shells out to the same command you would run by hand.

```bash
python factory.py --channel football_news \
  --allow-review-failures image_review,thumbnail_review,final_review \
  --set plan.topic="<the topic the user typed>"
```

## Running

```bash
cp worker/.env.example worker/.env    # then fill it in
python worker/run_worker.py
```

`--once` processes a single job and exits, which is handy for testing.

## Requirements

Everything [DEPLOYMENT.md](../DEPLOYMENT.md) lists for the pipeline — Python,
Node, FFmpeg, `npm install` in `rendering/remotion`, Google Cloud ADC, an
Arabic-capable font — plus the variables in `.env.example`.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `APP_URL` | — | The deployed Vercel app to poll |
| `WORKER_TOKEN` | — | Shared secret; must match the Vercel project's value |
| `MEDIA_STORAGE_PROVIDER` | `gcs` | Media backend: `gcs` or `supabase` (see `storage.py`) |
| `GCS_BUCKET` | — | Bucket for finished videos and thumbnails when the provider is `gcs` |
| `VIDEO_CHANNEL` | `football_news` | Channel to generate for |
| `POLL_INTERVAL_SECONDS` | `15` | Idle queue poll interval |
| `ALLOWED_REVIEW_FAILURES` | `image_review,thumbnail_review,final_review` | Gates that record a verdict without discarding a finished video |
| `WORKSPACE_RETENTION` | `3` | Local runs to keep before pruning |
| `MIN_FREE_DISK_GB` | `2.0` | Refuse to start a run below this headroom |

## Operational notes

- **Single instance only.** The queue has no compare-and-set, and two runs
  would fight over `workspace/`. On Cloud Run use
  `--concurrency 1 --max-instances 1`.
- **Disk.** Each run leaves ~200 MB in `workspace/`. The worker prunes to
  `WORKSPACE_RETENTION` before every job and refuses to start below
  `MIN_FREE_DISK_GB`, because a render that hits ENOSPC halfway through wastes
  the whole run.
- **Crash recovery.** A job whose worker dies is marked failed by the app after
  20 minutes without a progress update, so the queue cannot wedge.

## Deploying to Cloud Run

Cloud Run suits this well: up to 60-minute requests, and the pipeline's Google
credentials come from the attached service account, so no key file leaves GCP.

```bash
# From the repository root.
gcloud run deploy video-factory-worker \
  --source . \
  --region us-central1 \
  --no-allow-unauthenticated \
  --cpu 4 --memory 8Gi \
  --timeout 3600 \
  --concurrency 1 --max-instances 1 --min-instances 1 \
  --service-account video-factory-worker@PROJECT_ID.iam.gserviceaccount.com \
  --set-env-vars "APP_URL=https://your-app.vercel.app,VIDEO_CHANNEL=football_news,GOOGLE_PROJECT_ID=PROJECT_ID,GOOGLE_STT_LOCATION=us-central1,WORKSPACE_RETENTION=1" \
  --set-secrets "WORKER_TOKEN=worker-token:latest,BLOB_READ_WRITE_TOKEN=blob-token:latest,SERPER_API_KEY=serper-key:latest"
```

`--source .` builds `worker/Dockerfile` when you point Cloud Build at it; to
build explicitly:

```bash
docker build -f worker/Dockerfile -t gcr.io/PROJECT_ID/video-factory-worker .
docker push gcr.io/PROJECT_ID/video-factory-worker
```

The service account needs `roles/aiplatform.user` and `roles/speech.client`.
Store `WORKER_TOKEN`, `BLOB_READ_WRITE_TOKEN` and `SERPER_API_KEY` in Secret
Manager rather than passing them as plain env vars.

Because the worker polls outbound and serves no HTTP, keep `--min-instances 1`
so it is always alive, and leave it unauthenticated-free
(`--no-allow-unauthenticated`).
