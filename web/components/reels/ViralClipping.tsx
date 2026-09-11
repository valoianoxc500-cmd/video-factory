"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  CLIP_COUNTS,
  CLIP_LANGUAGES,
  CLIP_LENGTHS,
  defaultAutoClipOptions,
  safeClipError,
  type AutoClipOptions,
  type ViralClipResult,
} from "@/lib/clipping";

interface ClipAsset {
  id: string;
  title: string;
  duration_seconds: number | null;
  storage_path: string;
}

interface TaskState {
  id: string;
  status: "queued" | "running" | "done" | "failed";
  error: string;
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
}

function clock(value: number): string {
  const seconds = Math.max(0, Math.round(Number(value) || 0));
  const hours = Math.floor(seconds / 3600);
  const rest = seconds % 3600;
  const minutes = Math.floor(rest / 60);
  const tail = String(rest % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${tail}` : `${minutes}:${tail}`;
}

export function ViralClipping({
  initialAssets,
  initialTask,
}: {
  initialAssets: ClipAsset[];
  initialTask: TaskState | null;
}) {
  const relevantInitial = initialTask?.payload?.auto_clip ? initialTask : null;
  const [assets, setAssets] = useState(initialAssets);
  const [selectedId, setSelectedId] = useState("");
  const [sourceUrl, setSourceUrl] = useState("");
  const [options, setOptions] = useState<AutoClipOptions>(defaultAutoClipOptions);
  const [authorized, setAuthorized] = useState(false);
  const [task, setTask] = useState<TaskState | null>(relevantInitial);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadPct, setUploadPct] = useState(0);
  const [localUrl, setLocalUrl] = useState("");
  const [localName, setLocalName] = useState("");
  const [resultUrls, setResultUrls] = useState<Record<string, string>>({});
  const fileRef = useRef<HTMLInputElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const objectUrlRef = useRef("");

  const selected = assets.find((asset) => asset.id === selectedId) ?? null;
  const running = task?.status === "queued" || task?.status === "running";
  const clips = (Array.isArray(task?.result?.clips) ? task?.result.clips : []) as ViralClipResult[];
  const readyClips = clips.filter((clip) => ["done", "skipped"].includes(clip.status));
  const hasInput = Boolean(selectedId || sourceUrl.trim());

  useEffect(() => () => {
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
  }, []);

  const poll = useCallback(async () => {
    if (!task?.id) return;
    try {
      const response = await fetch(`/api/reels/clips?task=${encodeURIComponent(task.id)}`, {
        cache: "no-store",
      });
      const body = await response.json();
      if (Array.isArray(body.assets)) setAssets(body.assets);
      if (body.latest) setTask(body.latest);
    } catch {
      // The next poll resumes quietly.
    }
  }, [task?.id]);

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(poll, 4000);
    return () => clearInterval(timer);
  }, [running, poll]);

  useEffect(() => {
    if (task?.status !== "done" || !task.id || !readyClips.length) return;
    let live = true;
    Promise.all(readyClips.map(async (clip) => {
      try {
        const response = await fetch(
          `/api/reels/clips/media?task=${encodeURIComponent(task.id)}&clip=${encodeURIComponent(clip.id)}`,
          { cache: "no-store" },
        );
        const body = await response.json();
        return [clip.id, response.ok ? String(body.url ?? "") : ""] as const;
      } catch {
        return [clip.id, ""] as const;
      }
    })).then((rows) => {
      if (live) setResultUrls(Object.fromEntries(rows));
    });
    return () => { live = false; };
  }, [task?.id, task?.status, readyClips.length]);

  const chooseFile = useCallback(async (file: File | null) => {
    if (!file) return;
    setError("");
    setTask(null);
    setSourceUrl("");
    if (objectUrlRef.current) URL.revokeObjectURL(objectUrlRef.current);
    const preview = URL.createObjectURL(file);
    objectUrlRef.current = preview;
    setLocalUrl(preview);
    setLocalName(file.name);
    setSelectedId("");
    setUploading(true);
    setUploadPct(0);
    try {
      const signedResponse = await fetch("/api/reels/clips/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "sign",
          filename: file.name,
          contentType: file.type,
          size: file.size,
        }),
      });
      const signed = await signedResponse.json();
      if (!signedResponse.ok) throw new Error(signed.error || "Upload could not start.");
      await putWithProgress(signed.uploadUrl, file, signed.headers, setUploadPct);
      const savedResponse = await fetch("/api/reels/clips/upload", {
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
      const saved = await savedResponse.json();
      if (!savedResponse.ok) throw new Error(saved.error || "Upload could not be saved.");
      setAssets((current) => [saved.asset, ...current.filter((item) => item.id !== saved.asset.id)]);
      setSelectedId(saved.asset.id);
      setUploadPct(100);
    } catch (caught) {
      setError(safeClipError((caught as Error).message));
      setLocalUrl("");
      setLocalName("");
    } finally {
      setUploading(false);
    }
  }, []);

  function updateOption(key: keyof AutoClipOptions, value: string) {
    setOptions((current) => ({ ...current, [key]: value }));
  }

  async function createClips() {
    if (!hasInput || !authorized || uploading) return;
    setBusy(true);
    setError("");
    setResultUrls({});
    try {
      const response = await fetch("/api/reels/clips", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          assetId: selectedId || undefined,
          sourceUrl: selectedId ? undefined : sourceUrl.trim(),
          ownsOrPermitted: authorized,
          clipOptions: options,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "Viral clips could not be started.");
      setTask(body.task);
    } catch (caught) {
      setError(safeClipError((caught as Error).message));
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    setTask(null);
    setResultUrls({});
    setError("");
  }

  return (
    <div className="viral-clip-ws">
      {error && <p className="notice notice-error" role="alert">{error}</p>}

      {!running && task?.status !== "done" && (
        <section className="viral-clip-composer">
          <div className="viral-link-box">
            <span aria-hidden>↗</span>
            <input
              value={sourceUrl}
              onChange={(event) => {
                setSourceUrl(event.target.value);
                if (event.target.value) setSelectedId("");
              }}
              placeholder="Paste a video link..."
              aria-label="Video link"
            />
          </div>

          <div className="viral-or"><span>OR</span></div>

          <div
            className={`viral-upload${dragging ? " is-over" : ""}`}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragging(false);
              chooseFile(event.dataTransfer.files?.[0] ?? null);
            }}
          >
            {localUrl ? (
              <div className="viral-upload-preview">
                <video ref={videoRef} src={localUrl} controls playsInline />
                <div><b>{localName}</b><span>{uploading ? `Uploading ${uploadPct}%` : "Ready to analyze"}</span></div>
              </div>
            ) : (
              <>
                <strong>Drop your video here</strong>
                <span>MP4, MOV, WebM or MKV · up to 2GB</span>
                <button type="button" className="btn-ghost" onClick={() => fileRef.current?.click()}>
                  Upload Video
                </button>
              </>
            )}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="video/mp4,video/quicktime,video/webm,video/x-matroska,.mp4,.mov,.webm,.mkv"
            hidden
            onChange={(event) => chooseFile(event.target.files?.[0] ?? null)}
          />

          {assets.length > 0 && !localUrl && (
            <div className="viral-existing">
              <span>Or use an uploaded video</span>
              <div>{assets.slice(0, 5).map((asset) => (
                <button
                  type="button"
                  key={asset.id}
                  className={selectedId === asset.id ? "is-on" : ""}
                  onClick={() => { setSelectedId(asset.id); setSourceUrl(""); }}
                >
                  <b>{asset.title || "Untitled video"}</b>
                  <span>{clock(Number(asset.duration_seconds ?? 0))}</span>
                </button>
              ))}</div>
            </div>
          )}

          <div className="viral-options">
            <label>Language<select value={options.language} onChange={(e) => updateOption("language", e.target.value)}>
              {CLIP_LANGUAGES.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select></label>
            <label>Clip length<select value={options.length} onChange={(e) => updateOption("length", e.target.value)}>
              {CLIP_LENGTHS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select></label>
            <label>Number of clips<select value={options.count} onChange={(e) => updateOption("count", e.target.value)}>
              {CLIP_COUNTS.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}
            </select></label>
          </div>

          <label className="viral-rights">
            <input type="checkbox" checked={authorized} onChange={(e) => setAuthorized(e.target.checked)} />
            I own this video or have permission to create clips from it.
          </label>

          <button
            className="viral-create"
            type="button"
            disabled={!hasInput || !authorized || uploading || busy}
            onClick={createClips}
          >
            {busy ? "Starting…" : "Create Viral Clips"}
          </button>
        </section>
      )}

      {running && (
        <section className="viral-finding" aria-live="polite">
          <div className="viral-orbit"><span /></div>
          <h2>Finding your best clips...</h2>
          <p>Analyzing hooks, moments, speakers, and pacing.</p>
        </section>
      )}

      {task?.status === "failed" && (
        <section className="viral-finding">
          <h2>We couldn&apos;t finish these clips.</h2>
          <p>{safeClipError(task.error)}</p>
          <button className="btn-ghost" type="button" onClick={reset}>Try another video</button>
        </section>
      )}

      {task?.status === "done" && (
        <section className="viral-results">
          <div className="viral-results-head">
            <div><span>READY TO POST</span><h2>Your strongest moments</h2></div>
            <button className="btn-ghost" type="button" onClick={reset}>Create more clips</button>
          </div>
          <div className="viral-results-grid">
            {readyClips.map((clip) => (
              <article className="viral-result-card" key={clip.id}>
                <div className="viral-result-video">
                  {resultUrls[clip.id] ? <video src={resultUrls[clip.id]} controls playsInline /> : <div className="viral-media-loading">Preparing preview…</div>}
                  <span className="viral-score">{clip.viral_score}<small>VIRAL</small></span>
                </div>
                <div className="viral-result-body">
                  <span className="viral-reason">{clip.reason}</span>
                  <h3>{clip.title}</h3>
                  <p>{clock(clip.start)} — {clock(clip.end)} · {clock(clip.duration)}</p>
                  <div className="viral-result-actions">
                    <a className="btn-primary" href={resultUrls[clip.id] || undefined} download>Download</a>
                    <button type="button" className="btn-ghost" title="Advanced clip editing is coming next">Edit clip</button>
                  </div>
                </div>
              </article>
            ))}
          </div>
          {readyClips.length === 0 && <p className="notice">No complete standalone moments were found.</p>}
        </section>
      )}
    </div>
  );
}

function putWithProgress(
  url: string,
  file: File,
  headers: Record<string, string>,
  onProgress: (value: number) => void,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("PUT", url, true);
    Object.entries(headers ?? {}).forEach(([key, value]) => request.setRequestHeader(key, value));
    request.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(Math.round((event.loaded / event.total) * 100));
    };
    request.onload = () => request.status >= 200 && request.status < 300
      ? resolve()
      : reject(new Error("Upload did not complete."));
    request.onerror = () => reject(new Error("Upload was interrupted."));
    request.onabort = () => reject(new Error("Upload was cancelled."));
    request.send(file);
  });
}
