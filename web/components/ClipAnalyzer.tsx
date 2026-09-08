"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { AssetRow, TaskRow } from "@/lib/vrf";

/**
 * Clip Analyzer.
 *
 * Reads one video and explains it. There is deliberately no "make one like
 * this" button: the last panel is a plan a person carries out, and turning it
 * into a generation would be the Re-Create behaviour this replaced.
 */

/** The order the analysis reads in. Mirrors SECTIONS in viral/explain.py. */
const SECTIONS: [string, string][] = [
  ["why_it_performed", "Why it performed well"],
  ["hook", "Hook"],
  ["structure", "Structure"],
  ["pacing", "Pacing"],
  ["visuals", "Visuals"],
  ["captions", "Captions"],
  ["audio", "Audio"],
  ["improvements", "What to improve"],
];

type Explanation = {
  depth?: string;
  headline?: string;
  sections?: Record<string, string>;
  plan?: string[];
  cuts_per_minute?: number | null;
  frames_examined?: number;
  limitations?: string;
  title?: string;
};

export function ClipAnalyzer({
  assets,
  latest,
}: {
  assets: AssetRow[];
  latest: TaskRow | null;
}) {
  const [rows, setRows] = useState<AssetRow[]>(assets);
  const [selected, setSelected] = useState<string>(assets[0]?.id ?? "");
  const [task, setTask] = useState<TaskRow | null>(latest);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Adding a video by link. Goes through the existing assets route, which is
  // what carries the rights attestation -- analysing a video is still reading
  // someone's work, and the confirmation belongs on the way in rather than
  // being skipped because this screen only looks.
  const [url, setUrl] = useState("");
  const [owns, setOwns] = useState(false);
  const [adding, setAdding] = useState(false);

  const running = task?.status === "queued" || task?.status === "running";

  useEffect(() => {
    if (!task || !running) {
      if (pollRef.current) clearInterval(pollRef.current);
      return;
    }
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`/api/analyzer?task=${task.id}`, {
          cache: "no-store",
        });
        if (!res.ok) return;
        const data = (await res.json()) as { task?: TaskRow };
        if (data.task) setTask(data.task);
      } catch {
        /* transient */
      }
    }, 4000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [task, running]);

  const analyse = useCallback(async () => {
    if (!selected || busy || running) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/analyzer", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ assetId: selected }),
      });
      const data = (await res.json()) as Record<string, unknown>;
      if (!res.ok) {
        setError(String(data.error ?? `Could not start (${res.status}).`));
        return;
      }
      setTask(data.task as TaskRow);
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }, [selected, busy, running]);

  const addByUrl = useCallback(async () => {
    const link = url.trim();
    if (!link || !owns || adding || running) return;
    setAdding(true);
    setError(null);
    try {
      const res = await fetch("/api/reels/assets", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url: link, ownsOrPermitted: true }),
      });
      const data = (await res.json()) as Record<string, unknown>;
      if (!res.ok) {
        setError(String(data.error ?? `Could not add that link (${res.status}).`));
        return;
      }
      const asset = data.asset as AssetRow;
      setRows((prev) => [asset, ...prev]);
      setSelected(asset.id);
      setUrl("");
      setOwns(false);
    } catch {
      setError("Could not reach the server.");
    } finally {
      setAdding(false);
    }
  }, [url, owns, adding, running]);

  const result: Explanation =
    task?.status === "done" ? ((task.result ?? {}) as Explanation) : {};
  const hasResult = Boolean(result.headline || result.sections);
  const metadataOnly = result.depth === "metadata";

  return (
    <>
      {error && <div className="notice notice-error">{error}</div>}

      <div className="studio">
        <p className="studio-sub">
          Pick a video you have imported. The analyser samples its frames,
          measures its cut rate, and explains what made it hold attention —
          then gives you a plan to make one of your own. It never rebuilds the
          video for you.
        </p>

        {/* Provide a video. The link goes through the same route My Videos
            uses, so the rights confirmation is the same one, asked once. */}
        <div className="opt-group">
          <p className="opt-label">Add a video by link</p>
          <div className="add-row">
            <input
              type="url"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void addByUrl();
              }}
              placeholder="https://www.youtube.com/watch?v=…"
              disabled={adding || running}
              aria-label="Video link"
            />
            <button
              type="button"
              className="btn-ghost"
              onClick={() => void addByUrl()}
              disabled={!url.trim() || !owns || adding || running}
            >
              {adding ? "Adding…" : "Add"}
            </button>
          </div>
          <label className="attest">
            <input
              type="checkbox"
              checked={owns}
              onChange={(e) => setOwns(e.target.checked)}
              disabled={adding || running}
            />
            <span>I own this content or have permission to reuse it.</span>
          </label>
        </div>

        {rows.length === 0 ? (
          <div className="empty">
            <h3>Nothing to analyse yet</h3>
            <p>
              Paste a link above, or import a video under Clipping. The
              analyser reads videos you hold the rights to.
            </p>
          </div>
        ) : (
          <>
            <div className="opt-group">
              <p className="opt-label" id="asset-label">
                Video
              </p>
              <div className="asset-list" role="radiogroup" aria-labelledby="asset-label">
                {rows.map((a) => {
                  const on = selected === a.id;
                  const hasFile = Boolean(
                    String(a.processed_path ?? "").trim() ||
                      String(a.storage_path ?? "").trim(),
                  );
                  return (
                    <button
                      key={a.id}
                      type="button"
                      role="radio"
                      aria-checked={on}
                      onClick={() => setSelected(a.id)}
                      className={`asset-row${on ? " is-on" : ""}`}
                      disabled={running}
                    >
                      {a.thumbnail_url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={a.thumbnail_url} alt="" className="asset-thumb" />
                      ) : (
                        <span className="asset-thumb asset-thumb-empty" aria-hidden />
                      )}
                      <span className="asset-meta">
                        <span className="asset-title">{a.title || "Untitled"}</span>
                        <span className="asset-sub">
                          {a.source_platform || "imported"}
                          {a.source_author ? ` · ${a.source_author}` : ""}
                          {hasFile ? " · frames available" : " · signals only"}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
              <p className="opt-hint">
                A video with a file is examined frame by frame. One without is
                read from public signals alone, and the analysis says so.
              </p>
            </div>

            <button
              className="btn-generate"
              type="button"
              onClick={() => void analyse()}
              disabled={busy || running || !selected}
            >
              <svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden>
                <path
                  d="M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm10 17-5.2-5.2"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                />
              </svg>
              {running ? "Analysing…" : busy ? "Starting…" : "Analyse this video"}
            </button>
          </>
        )}
      </div>

      {running && (
        <div className="progress-card">
          <div className="progress-top">
            <span className="spinner" aria-hidden />
            <div className="progress-meta">
              <span className="chip chip-run">Working</span>
              <h3>Reading the video</h3>
              <p>Sampling frames, measuring the cut rate, and writing it up.</p>
            </div>
          </div>
        </div>
      )}

      {task?.status === "failed" && (
        <div className="notice notice-error">
          {task.error || "The analysis could not be completed."}
        </div>
      )}

      {task?.status === "done" && hasResult && (
        <div className="analysis">
          <div className="analysis-head">
            <span className={`chip ${metadataOnly ? "chip-warn" : "chip-ok"}`}>
              {metadataOnly ? "Signals only" : `${result.frames_examined ?? 0} frames examined`}
            </span>
            {typeof result.cuts_per_minute === "number" && (
              <span className="chip chip-run">
                {result.cuts_per_minute.toFixed(1)} cuts/min
              </span>
            )}
            {result.title && <span className="analysis-title">{result.title}</span>}
          </div>

          {result.headline && <p className="analysis-lead">{result.headline}</p>}

          <div className="analysis-grid">
            {SECTIONS.map(([key, label]) => {
              const text = result.sections?.[key];
              if (!text) return null;
              return (
                <section className="analysis-card" key={key}>
                  <h3>{label}</h3>
                  <p>{text}</p>
                </section>
              );
            })}
          </div>

          {result.plan && result.plan.length > 0 && (
            <section className="analysis-plan">
              <h3>How to make one like it</h3>
              <ol>
                {result.plan.map((step, i) => (
                  <li key={i}>{step}</li>
                ))}
              </ol>
              <p className="analysis-note">
                This is a plan, not a queue. Nothing here has been generated —
                carry the steps out yourself, or start a fresh video from one
                of the story engines.
              </p>
            </section>
          )}

          {result.limitations && (
            <p className="analysis-limits">
              <strong>Not assessed:</strong> {result.limitations}
            </p>
          )}
        </div>
      )}
    </>
  );
}
