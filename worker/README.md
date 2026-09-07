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

### As a Windows scheduled task

Both workers install the same way, one script each. They run at logon as the
current user — the pipeline authenticates to Google with Application Default
Credentials from that user's profile, so SYSTEM would not find them, which also
rules out an at-startup trigger.

```powershell
powershell -ExecutionPolicy Bypass -File worker\install_windows_service.ps1
powershell -ExecutionPolicy Bypass -File worker\install_vrf_windows_service.ps1
```

Each takes `-Uninstall` to remove its task. `VideoFactoryWorker` runs
`worker\run_worker.cmd`; `ViralReelsFinderWorker` runs
`worker\run_vrf_worker.cmd` (note `vrf_worker.py` lives at the repository root,
not under `worker\`, though it reads the same `worker\.env`).

**Recovery is a repeating trigger, not "restart the task if it fails."** That
setting does not fire for an action that returns non-zero — verified by killing
the worker and watching the task sit at `LastTaskResult -1` for 200 seconds
without restarting. Both installers add a `Once` trigger with a start time in
the past that repeats every minute indefinitely; a dead worker comes back
within the minute. Every repeat while the worker is healthy is a no-op,
because `MultipleInstances` is `IgnoreNew` — and the kernel lock refuses
anything that gets past that.

The launchers hand the worker's exit code back to the scheduler
(`endlocal & exit /b %RC%`). Without that the script's exit code is its final
`echo`, always 0, so a worker that died reported success.

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

- **Single instance only, and enforced.** The queue has no compare-and-set, and
  two runs would fight over `workspace/`. Each worker takes an OS-level lock at
  startup — `workspace/.worker.lock` here, `workspace/.vrf_worker.lock` for
  `vrf_worker.py` — and a second copy exits 2 with

  ```
  another video worker is already running (pid 1234, lock: ...\workspace\.worker.lock).
  ```

  The lock is held by the kernel (`msvcrt.locking` on Windows, `flock`
  elsewhere), so it is released automatically when the worker exits, is killed,
  or the machine loses power. There is no stale lock to clear by hand, and a
  restart straight after a stop waits out the few hundred milliseconds Windows
  takes to drop a dead process's locks.

  This replaced a PID file that was read, checked, then written — two workers
  launched in the same second would both see a file nobody held and both start.
  Do not reintroduce a PID liveness probe — see `worker/singleton.py`.

  The two workers hold *different* locks and are meant to run side by side.
  On Cloud Run use `--concurrency 1 --max-instances 1`.

- **Counting worker processes on Windows.** `.venv\Scripts\python.exe` in this
  checkout is a redirector: it re-launches the base interpreter and waits. Each
  worker therefore appears **twice** in the process list — a `.venv` stub and a
  `Python310` child — and two workers look like four. The child is the one
  running the code and holding the lock, so `.worker.lock` names the child's
  pid, not the pid you launched. Count workers by lock, or by `run_worker.py`
  processes whose executable is under `.venv`, not by raw process count.

  Stop the child (or the whole tree); killing only the `.venv` stub can leave
  the real interpreter running and still holding the lock.
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
