"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  ASPECTS,
  CAPTION_STYLES,
  FOCUS_MODES,
  QUALITIES,
  customerState,
  defaultClipOptions,
  safeClipError,
  type ClipOptions,
} from "@/lib/clipping";

/**
 * The Clipping workspace: upload, configure, create, download — one screen.
 *
 * It used to open on "Nothing to clip yet" and send the user to My Videos,
 * which adds a video by *link*. A YouTube link yields metadata and no file,
 * so the round trip could not produce a clippable video at all and the screen
 * was permanently empty. Uploading is now the primary path and lives here.
 *
 * The file goes browser -> storage directly (see `lib/uploads.ts`); this
 * component only ever holds a signed URL and a progress number. The local
 * preview is an object URL, so scrubbing and trimming work the instant the
 * file is chosen rather than after a round trip.
 *
 * Everything the customer sees about a running job comes from `customerState`.
 * The task row's own words never reach the screen.
 */

interface ClipAsset {
  id: string;
  title: string;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  thumbnail_url: string;
  storage_path: string;
  processed_path: string;
  ingest_status: string;
  ingest_detail: string;
}

interface TaskState {
  id: string;
  status: "queued" | "running" | "done" | "failed";
  error: string;
  payload: Record<string, unknown>;
}

const STAGES = ["Preparing", "Analyzing", "Creating clip", "Rendering", "Ready"] as const;

/** A video with no imported file cannot be clipped, whatever else it has. */
function isProcessable(asset: ClipAsset): boolean {
  return Boolean(String(asset.storage_path ?? "").trim());
}

function clock(value: number): string {
  if (!Number.isFinite(value) || value <= 0) return "0:00";
  const whole = Math.round(value);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

export function Clipping({
  initialAssets,
  initialTask,
}: {
  initialAssets: ClipAsset[];
  initialTask: TaskState | null;
}) {
  const [assets, setAssets] = useState<ClipAsset[]>(initialAssets);
  const [selectedId, setSelectedId] = useState<string>("");

  // Local file state, before and during upload.
  const [localUrl, setLocalUrl] = useState("");
  const [localName, setLocalName] = useState("");
  const [uploading, setUploading] = useState(false);
  const [uploadPct, setUploadPct] = useState(0);
  const [dragging, setDragging] = useState(false);

  const [duration, setDuration] = useState(0);
  const [trimStart, setTrimStart] = useState(0);
  const [trimEnd, setTrimEnd] = useState(0);
  const [options, setOptions] = useState<ClipOptions>(defaultClipOptions);

  // Signed, expiring URLs for the chosen source and the finished clip.
  // Never the stored object URL: that would require a public bucket.
  const [sourceUrl, setSourceUrl] = useState("");
  const [resultUrl, setResultUrl] = useState("");
  const [task, setTask] = useState<TaskState | null>(initialTask);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  // Stated literally rather than imported from `lib/uploads`: that module
  // pulls in node:crypto for signing and cannot cross into a client bundle.
  // It is asserted against the real limit in tests so the two cannot drift.
  const limits = "MP4, MOV, WebM or MKV · up to 2GB";

  const fileRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const objectUrlRef = useRef<string>("");

  const selected = assets.find((a) => a.id === selectedId) ?? null;
  const running = task?.status === "queued" || task?.status === "running";
  const state = customerState(task?.status);

  // A clip needs a video that actually has a file behind it.
  const ready = Boolean(selected && isProcessable(selected)) && !uploading;
  const clipLength = Math.max(0, duration - trimStart - trimEnd);
  // The local object URL wins while a file is still in the browser: it
  // needs no network and no signature.
  const previewSrc = localUrl || sourceUrl;

  function setOption<K extends keyof ClipOptions>(key: K, value: ClipOptions[K]) {
    setOptions((prev) => ({ ...prev, [key]: value }));
  }

  useEffect(() => {
    return () => {
      if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    };
  }, []);

  const signMedia = useCallback(
    async (assetId: string, kind: "source" | "processed") => {
      try {
        const response = await fetch(
          `/api/reels/clips/media?asset=${encodeURIComponent(assetId)}&kind=${kind}`,
          { cache: "no-store" },
        );
        const body = await response.json();
        return response.ok ? String(body.url ?? "") : "";
      } catch {
        return "";
      }
    },
    [],
  );

  // A stored asset needs a signed URL before it can be played. A file that
  // is still local does not, so this does nothing until one is committed.
  useEffect(() => {
    if (!selectedId || localUrl) {
      setSourceUrl("");
      return;
    }
    let live = true;
    signMedia(selectedId, "source").then((url) => {
      if (live) setSourceUrl(url);
    });
    return () => {
      live = false;
    };
  }, [selectedId, localUrl, signMedia]);

  // ── polling ────────────────────────────────────────────────────────

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/reels/clips", { cache: "no-store" });
      const body = await response.json();
      if (Array.isArray(body.assets)) setAssets(body.assets);
      if (body.latest) setTask(body.latest);
    } catch {
      // A dropped poll is not worth a message; the next one recovers.
    }
  }, []);

  useEffect(() => {
    if (!running) {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = null;
      return;
    }
    pollRef.current = setInterval(refresh, 4000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [running, refresh]);

  // ── upload ─────────────────────────────────────────────────────────

  const chooseFile = useCallback(async (file: File | null) => {
    if (!file) return;
    setError("");

    // Show it immediately from the local file. Nothing has been uploaded yet,
    // and the trim controls should not wait on a network round trip.
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    const url = URL.createObjectURL(file);
    objectUrlRef.current = url;
    setLocalUrl(url);
    setLocalName(file.name);
    setSelectedId("");
    setTrimStart(0);
    setTrimEnd(0);
    setTask(null);

    setUploading(true);
    setUploadPct(0);
    try {
      const signRes = await fetch("/api/reels/clips/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "sign",
          filename: file.name,
          contentType: file.type,
          size: file.size,
        }),
      });
      const signed = await signRes.json();
      if (!signRes.ok) throw new Error(signed.error || "Upload could not start.");

      await putWithProgress(signed.uploadUrl, file, signed.headers, setUploadPct);

      const commitRes = await fetch("/api/reels/clips/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "commit",
          objectPath: signed.objectPath,
          title: file.name,
          ownsOrPermitted: true,
          durationSeconds: videoRef.current?.duration ?? 0,
        }),
      });
      const committed = await commitRes.json();
      if (!commitRes.ok) throw new Error(committed.error || "Upload could not be saved.");

      const asset = committed.asset as ClipAsset;
      setAssets((prev) => [asset, ...prev.filter((a) => a.id !== asset.id)]);
      setSelectedId(asset.id);
      setUploadPct(100);
    } catch (err) {
      setError(safeClipError((err as Error).message));
      setLocalUrl("");
      setLocalName("");
    } finally {
      setUploading(false);
    }
  }, []);

  function replaceVideo() {
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    objectUrlRef.current = "";
    setLocalUrl("");
    setLocalName("");
    setSelectedId("");
    setDuration(0);
    setTrimStart(0);
    setTrimEnd(0);
    setTask(null);
    setError("");
    setUploadPct(0);
  }

  // ── create ─────────────────────────────────────────────────────────

  async function createClip() {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/reels/clips", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          assetId: selected.id,
          platform: "tiktok",
          trimStart,
          trimEnd,
          clipOptions: options,
        }),
      });
      const body = await response.json();
      if (!response.ok) {
        setError(safeClipError(body.error || "Could not start clipping."));
        return;
      }
      setTask(body.task);
    } catch {
      setError("Could not reach the server. Check your connection.");
    } finally {
      setBusy(false);
    }
  }

  // ── result ─────────────────────────────────────────────────────────

  // The finished clip is signed the same way. `processed_path` only tells
  // us the clip exists; it is never used as a src.
  const hasResult =
    task?.status === "done" &&
    Boolean(String(selected?.processed_path ?? "").trim());

  useEffect(() => {
    if (!hasResult || !selected) {
      setResultUrl("");
      return;
    }
    let live = true;
    signMedia(selected.id, "processed").then((url) => {
      if (live) setResultUrl(url);
    });
    return () => {
      live = false;
    };
  }, [hasResult, selected, signMedia]);

  const result = hasResult && resultUrl ? { url: resultUrl } : null;

  const outputAspect = ASPECTS.find((a) => a.id === options.aspect)?.label ?? "Vertical";

  // ── render ─────────────────────────────────────────────────────────

  const existing = assets.filter(isProcessable);

  return (
    <div className="clip-ws">
      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}

      <div className="clip-grid">
        {/* ── left: the video ──────────────────────────────────── */}
        <section className="clip-stage">
          {!previewSrc ? (
            <div
              className={`clip-drop${dragging ? " is-over" : ""}`}
              onDragOver={(e) => {
                e.preventDefault();
                setDragging(true);
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={(e) => {
                e.preventDefault();
                setDragging(false);
                chooseFile(e.dataTransfer.files?.[0] ?? null);
              }}
            >
              <span className="clip-drop-icon" aria-hidden>
                <svg width="34" height="34" viewBox="0 0 24 24" fill="none">
                  <path
                    d="M12 16V4m0 0L7 9m5-5 5 5M4 17v2a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-2"
                    stroke="currentColor"
                    strokeWidth="1.7"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              </span>
              <h3>Drop your video here</h3>
              <p>or choose a file</p>
              <button
                type="button"
                className="btn-primary"
                onClick={() => fileRef.current?.click()}
              >
                Upload Video
              </button>
              <p className="clip-limits">{limits}</p>
            </div>
          ) : (
            <div className="clip-player">
              <video
                ref={videoRef}
                src={previewSrc}
                controls
                playsInline
                preload="metadata"
                className="clip-video"
                onLoadedMetadata={(e) => {
                  const value = e.currentTarget.duration;
                  if (Number.isFinite(value) && value > 0) setDuration(value);
                }}
              />
              <div className="clip-player-bar">
                <span className="clip-player-name" title={localName || selected?.title}>
                  {localName || selected?.title || "Your video"}
                </span>
                <span className="clip-player-meta">{clock(duration)}</span>
                <button type="button" className="btn-ghost clip-replace" onClick={replaceVideo}>
                  Replace video
                </button>
              </div>

              {uploading && (
                <div className="clip-upload">
                  <div className="clip-upload-bar" style={{ width: `${uploadPct}%` }} />
                  <span>Uploading… {uploadPct}%</span>
                </div>
              )}
            </div>
          )}

          <input
            ref={fileRef}
            type="file"
            accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv"
            hidden
            onChange={(e) => chooseFile(e.target.files?.[0] ?? null)}
          />

          {/* Previously uploaded videos, if any. Never metadata-only ones. */}
          {existing.length > 0 && !localUrl && (
            <div className="clip-recent">
              <span className="opt-label">Or use a video you already uploaded</span>
              <div className="clip-recent-row">
                {existing.slice(0, 6).map((asset) => (
                  <button
                    key={asset.id}
                    type="button"
                    className={`clip-chip${selectedId === asset.id ? " is-on" : ""}`}
                    onClick={() => {
                      setSelectedId(asset.id);
                      setDuration(Number(asset.duration_seconds ?? 0));
                      setTrimStart(0);
                      setTrimEnd(0);
                      setTask(null);
                    }}
                  >
                    {asset.title || "Untitled"}
                    <span>{clock(Number(asset.duration_seconds ?? 0))}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </section>

        {/* ── right: the settings ──────────────────────────────── */}
        <aside className="clip-panel">
          <h2 className="clip-panel-h">Clip settings</h2>

          <div className="opt-group">
            <span className="opt-label">Trim</span>
            {duration > 0 ? (
              <>
                <label className="clip-slider">
                  <span>
                    Start <b>{clock(trimStart)}</b>
                  </span>
                  <input
                    type="range"
                    min={0}
                    max={Math.max(0, Math.floor(duration) - 1)}
                    value={trimStart}
                    onChange={(e) => setTrimStart(Number(e.target.value))}
                  />
                </label>
                <label className="clip-slider">
                  <span>
                    End <b>{clock(Math.max(0, duration - trimEnd))}</b>
                  </span>
                  <input
                    type="range"
                    min={0}
                    max={Math.max(0, Math.floor(duration) - 1)}
                    value={trimEnd}
                    onChange={(e) => setTrimEnd(Number(e.target.value))}
                  />
                </label>
                <p className="clip-length">
                  Clip length <b>{clock(clipLength)}</b>
                  <span> of {clock(duration)}</span>
                </p>
              </>
            ) : (
              <p className="opt-hint">
                Add a video and its length will appear here.
              </p>
            )}
          </div>

          <div className="opt-group">
            <span className="opt-label">Format</span>
            <div className="clip-cards">
              {ASPECTS.map((a) => (
                <button
                  key={a.id}
                  type="button"
                  className={`clip-card${options.aspect === a.id ? " is-on" : ""}`}
                  onClick={() => setOption("aspect", a.id)}
                >
                  <span className={`clip-ratio r-${a.id.replace(":", "-")}`} aria-hidden />
                  <b>{a.label}</b>
                  <span>{a.hint}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="opt-group">
            <span className="opt-label">Framing</span>
            <div className="seg seg-wrap">
              {FOCUS_MODES.map((f) => (
                <button
                  key={f.id}
                  type="button"
                  className={`seg-item${options.focus === f.id ? " seg-on" : ""}`}
                  onClick={() => setOption("focus", f.id)}
                >
                  {f.label}
                </button>
              ))}
            </div>
            <p className="opt-hint">
              Follow Subject keeps the person in frame. If nobody can be found
              the crop stays centred rather than guessing.
            </p>
          </div>

          <div className="opt-group">
            <div className="clip-toggle-row">
              <span className="opt-label" style={{ margin: 0 }}>
                Captions
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={options.captions}
                className={`clip-switch${options.captions ? " is-on" : ""}`}
                onClick={() => setOption("captions", !options.captions)}
              >
                <span />
              </button>
            </div>
            {options.captions && (
              <>
                <div className="seg seg-wrap" style={{ marginTop: 10 }}>
                  {CAPTION_STYLES.map((c) => (
                    <button
                      key={c.id}
                      type="button"
                      className={`seg-item${options.caption_style === c.id ? " seg-on" : ""}`}
                      onClick={() => setOption("caption_style", c.id)}
                    >
                      {c.label}
                    </button>
                  ))}
                </div>
                <label className="clip-field">
                  <span>Speaker focus (optional)</span>
                  <input
                    value={options.speaker}
                    maxLength={80}
                    placeholder="e.g. the woman in the red jacket"
                    onChange={(e) => setOption("speaker", e.target.value)}
                  />
                </label>
              </>
            )}
            <p className="opt-hint">
              Captions are written from the clip&apos;s own audio. If they
              cannot be produced the clip is still made, without them.
            </p>
          </div>

          <div className="opt-group">
            <span className="opt-label">Quality</span>
            <div className="seg seg-wrap">
              {QUALITIES.map((q) => (
                <button
                  key={q.id}
                  type="button"
                  className={`seg-item${options.quality === q.id ? " seg-on" : ""}`}
                  onClick={() => setOption("quality", q.id)}
                >
                  {q.label}
                </button>
              ))}
            </div>
            <p className="opt-hint">
              {QUALITIES.find((q) => q.id === options.quality)?.hint}
            </p>
          </div>

          <button
            className="btn-generate clip-create"
            type="button"
            onClick={createClip}
            disabled={busy || running || !ready || (duration > 0 && clipLength < 1)}
          >
            {busy ? "Starting…" : running ? "Working…" : "Create Clip"}
          </button>
          {!ready && !uploading && (
            <p className="opt-hint" style={{ textAlign: "center" }}>
              Add a video to start.
            </p>
          )}
        </aside>
      </div>

      {/* ── progress ─────────────────────────────────────────────── */}
      {task && task.status !== "failed" && (
        <section className="clip-progress">
          <ol className="clip-stages">
            {STAGES.map((label) => {
              const index = STAGES.indexOf(label);
              const current = state ? STAGES.indexOf(state) : 0;
              const done = index < current;
              const now = index === current;
              return (
                <li
                  key={label}
                  className={done ? "is-done" : now ? "is-now" : ""}
                >
                  <span className="clip-stage-dot" aria-hidden />
                  {label}
                </li>
              );
            })}
          </ol>
          {state !== "Ready" && (
            <p className="opt-hint">
              This runs on the worker and keeps going if you leave the page.
            </p>
          )}
        </section>
      )}

      {task?.status === "failed" && (
        <p className="notice notice-error">{safeClipError(task.error)}</p>
      )}

      {/* ── result ───────────────────────────────────────────────── */}
      {result && (
        <section className="clip-result">
          <h2 className="clip-panel-h">Your clip</h2>
          <div className="clip-result-grid">
            <video src={result.url} controls playsInline className="clip-video" />
            <div className="clip-result-facts">
              <dl>
                <div>
                  <dt>Original</dt>
                  <dd>{clock(duration)}</dd>
                </div>
                <div>
                  <dt>Clip</dt>
                  <dd>{clock(clipLength)}</dd>
                </div>
                <div>
                  <dt>Format</dt>
                  <dd>
                    {options.aspect} · {outputAspect}
                  </dd>
                </div>
                <div>
                  <dt>Captions</dt>
                  <dd>{options.captions ? "Burned in" : "Off"}</dd>
                </div>
              </dl>
              <a className="btn-primary" href={result.url} download>
                Download
              </a>
              <button
                type="button"
                className="btn-ghost"
                onClick={() => setTask(null)}
              >
                Create another version
              </button>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}

/**
 * PUT the file to storage, reporting progress.
 *
 * XMLHttpRequest rather than fetch: fetch still has no upload progress event
 * in browsers, and a 2GB upload with no progress bar reads as a hang.
 */
function putWithProgress(
  url: string,
  file: File,
  headers: Record<string, string>,
  onProgress: (pct: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("PUT", url, true);
    for (const [key, value] of Object.entries(headers ?? {})) {
      xhr.setRequestHeader(key, value);
    }
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100));
      }
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) resolve();
      // The storage response body is not shown to the customer; the caller
      // turns this into a sentence.
      else reject(new Error("Upload did not complete."));
    };
    xhr.onerror = () => reject(new Error("Upload was interrupted."));
    xhr.onabort = () => reject(new Error("Upload was cancelled."));
    xhr.send(file);
  });
}
