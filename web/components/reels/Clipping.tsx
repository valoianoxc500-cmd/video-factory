"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { PLATFORMS } from "@/lib/vrf";
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
 * Cut a section out of a video you own and reframe it to 9:16.
 *
 * Everything here maps onto work the worker already does: the trim and the
 * reframe are `viral/processing.py`, and the status shown is the real task
 * row, not a simulated one. There is no button that does nothing -- a video
 * with no imported file cannot be clipped, and says so instead of offering a
 * control that would fail.
 */

interface ClipAsset {
  id: string;
  title: string;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  thumbnail_url: string;
  source_platform: string;
  source_author: string;
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

const CLIP_PLATFORMS = PLATFORMS.filter((p) => p.supported);

function seconds(value: number): string {
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
  const [selectedId, setSelectedId] = useState<string>(
    initialAssets[0]?.id ?? "",
  );
  const [platform, setPlatform] = useState<string>("tiktok");
  const [trimStart, setTrimStart] = useState(0);
  const [trimEnd, setTrimEnd] = useState(0);
  const [options, setOptions] = useState<ClipOptions>(defaultClipOptions);

  function setOption<K extends keyof ClipOptions>(key: K, value: ClipOptions[K]) {
    setOptions((prev) => ({ ...prev, [key]: value }));
  }
  const [task, setTask] = useState<TaskState | null>(initialTask);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const selected = assets.find((a) => a.id === selectedId) ?? null;
  const duration = Number(selected?.duration_seconds ?? 0);
  const remaining = Math.max(0, duration - trimStart - trimEnd);
  const running = task?.status === "queued" || task?.status === "running";

  // Reset the trims whenever the chosen video changes: seconds from one
  // video's timeline mean nothing on another's.
  useEffect(() => {
    setTrimStart(0);
    setTrimEnd(0);
  }, [selectedId]);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch("/api/reels/clips", { cache: "no-store" });
      const body = await response.json();
      if (Array.isArray(body.assets)) setAssets(body.assets);
      if (body.latest) setTask(body.latest);
    } catch {
      // A failed poll is not worth interrupting the screen for; the next one
      // will either succeed or the task will still read as running.
    }
  }, []);

  // Poll only while there is something to watch.
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
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

  async function makeClip() {
    if (!selected) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/reels/clips", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          assetId: selected.id,
          platform,
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

  if (assets.length === 0) {
    return (
      <div className="empty">
        <h3>Nothing to clip yet</h3>
        <p>
          Clipping works on videos you own. Add one under My Videos — paste the
          link and confirm the rights — and once its file has imported it will
          appear here ready to cut.
        </p>
        <Link href="/dashboard/reels/videos" className="btn-primary">
          Add a video
        </Link>
      </div>
    );
  }

  return (
    <>
      <div className="opt-group">
        <span className="opt-label">Video</span>
        <div className="chan-grid">
          {assets.map((asset) => {
            const on = asset.id === selectedId;
            return (
              <button
                key={asset.id}
                type="button"
                className={`chan${on ? " is-on" : ""}`}
                onClick={() => setSelectedId(asset.id)}
                style={
                  asset.thumbnail_url
                    ? {
                        ["--head-art" as string]: `url('${asset.thumbnail_url}')`,
                      }
                    : undefined
                }
              >
                {on && <span className="chan-check" aria-hidden />}
                <h3>{asset.title || "Untitled video"}</h3>
                <p>
                  {seconds(Number(asset.duration_seconds ?? 0))}
                  {asset.source_author ? ` · ${asset.source_author}` : ""}
                  {asset.width && asset.height
                    ? ` · ${asset.width}×${asset.height}`
                    : ""}
                </p>
              </button>
            );
          })}
        </div>
      </div>

      <div className="opt-group">
        <span className="opt-label">Cut to</span>
        <div className="seg">
          {CLIP_PLATFORMS.map((p) => (
            <button
              key={p.platform}
              type="button"
              className={`seg-item${platform === p.platform ? " seg-on" : ""}`}
              onClick={() => setPlatform(p.platform)}
            >
              {p.label}
            </button>
          ))}
        </div>
        <p className="opt-hint">
          Sets the encode target. The picture is reframed to 9:16 around the
          subject, and loudness is normalised to the platform&apos;s spec.
        </p>
      </div>

      <div className="opt-group">
        <span className="opt-label">Format</span>
        <div className="seg">
          {ASPECTS.map((a) => (
            <button
              key={a.id}
              type="button"
              className={`seg-item${options.aspect === a.id ? " seg-on" : ""}`}
              onClick={() => setOption("aspect", a.id)}
              title={a.hint}
            >
              {a.label}
            </button>
          ))}
        </div>
      </div>

      <div className="opt-group">
        <span className="opt-label">Framing</span>
        <div className="seg">
          {FOCUS_MODES.map((f) => (
            <button
              key={f.id}
              type="button"
              className={`seg-item${options.focus === f.id ? " seg-on" : ""}`}
              onClick={() => setOption("focus", f.id)}
              title={f.hint}
            >
              {f.label}
            </button>
          ))}
        </div>
        <p className="opt-hint">
          Follow subject keeps the person in frame. If the subject cannot be
          detected the crop stays centred rather than guessing.
        </p>
      </div>

      <div className="opt-group">
        <span className="opt-label">Captions</span>
        <div className="seg">
          <button
            type="button"
            className={`seg-item${!options.captions ? " seg-on" : ""}`}
            onClick={() => setOption("captions", false)}
          >
            Off
          </button>
          <button
            type="button"
            className={`seg-item${options.captions ? " seg-on" : ""}`}
            onClick={() => setOption("captions", true)}
          >
            On
          </button>
        </div>
        {options.captions && (
          <div className="seg" style={{ marginTop: 8 }}>
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
        )}
        <p className="opt-hint">
          Captions are transcribed from the clip&apos;s own audio. If the
          transcript cannot be produced the clip is still made, without them.
        </p>
      </div>

      <div className="opt-group">
        <span className="opt-label">Export quality</span>
        <div className="seg">
          {QUALITIES.map((q) => (
            <button
              key={q.id}
              type="button"
              className={`seg-item${options.quality === q.id ? " seg-on" : ""}`}
              onClick={() => setOption("quality", q.id)}
              title={q.hint}
            >
              {q.label}
            </button>
          ))}
        </div>
      </div>

      <div className="opt-group">
        <span className="opt-label">Trim</span>
        {duration > 0 ? (
          <>
            <div className="trim-row">
              <label className="trim-field">
                <span>
                  From start <b>{seconds(trimStart)}</b>
                </span>
                <input
                  type="range"
                  min={0}
                  max={Math.max(0, Math.floor(duration) - 1)}
                  value={trimStart}
                  onChange={(e) => setTrimStart(Number(e.target.value))}
                />
              </label>
              <label className="trim-field">
                <span>
                  From end <b>{seconds(trimEnd)}</b>
                </span>
                <input
                  type="range"
                  min={0}
                  max={Math.max(0, Math.floor(duration) - 1)}
                  value={trimEnd}
                  onChange={(e) => setTrimEnd(Number(e.target.value))}
                />
              </label>
            </div>
            <p className="opt-hint">
              Clip length <b>{seconds(remaining)}</b> of {seconds(duration)}.
              {remaining < 1 && " That leaves nothing to encode."}
            </p>
          </>
        ) : (
          <p className="opt-hint">
            This video&apos;s length is not known yet, so the whole file will be
            reframed. Trimming becomes available once it has been measured.
          </p>
        )}
      </div>

      {error && <div className="notice notice-error">{error}</div>}

      {task && (
        <div className={`notice${task.status === "failed" ? " notice-error" : ""}`}>
          {/* One of five states, never the task row's own words. A new
              internal stage must not become a new customer state. */}
          {task.status === "failed"
            ? safeClipError(task.error)
            : `${customerState(task.status) ?? "Preparing"}${
                task.status === "done"
                  ? " — your clip is on the video in My Videos."
                  : "…"
              }`}
        </div>
      )}

      <button
        className="btn-generate"
        type="button"
        onClick={makeClip}
        disabled={busy || running || !selected || (duration > 0 && remaining < 1)}
      >
        {busy ? "Starting…" : running ? "Clipping…" : "Make the clip"}
      </button>
    </>
  );
}
