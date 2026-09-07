"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  DEFAULT_ENGINE,
  ENGINES,
  defaultLanguage,
  defaultStyle,
  engineBySlug,
} from "@/lib/engines";
import type { JobRow } from "@/lib/repositories";

/**
 * The generation workflow.
 *
 * Deliberately the same shape the pipeline already expects -- engine slug,
 * topic, and the Horror story type -- so nothing about how a video is produced
 * changes. What changed is that the job is created against the signed-in user
 * and progress is read from their own jobs only.
 */

/**
 * How long the view will show progress with nothing changing.
 *
 * Above the worker's heartbeat and the server's expiry window, so this only
 * fires when both of those have already failed.
 */
const STALL_AFTER_MS = 18 * 60 * 1000;

/**
 * Each channel's card art.
 *
 * A colour field and a glyph rather than a photograph: it renders instantly,
 * stays legible at any card size, and does not put someone else's footage in
 * the chrome of a product about verified sourcing. Keyed by engine slug, with
 * a neutral fallback so a new channel appears without needing art first.
 */
const CHANNEL_ART: Record<
  string,
  { glyph: string; blurb: string; field: string }
> = {
  horror_stories: {
    glyph: "👻",
    blurb: "True scary stories, mysteries and paranormal events.",
    field: "url('/channels/horror_stories.jpg')",
  },
  football_news: {
    glyph: "⚽",
    blurb: "Latest football news, transfers and match analysis.",
    field: "url('/channels/football_news.jpg')",
  },
  _default: {
    glyph: "🎬",
    blurb: "Research, script, narrate and render a finished video.",
    field: "url('/channels/create.jpg')",
  },
};

/** The four things the pipeline promises, in the order it does them. */
const ASSURANCES = [
  ["Verified sources", "Real information only", "M21 21l-4.3-4.3M11 19a8 8 0 1 1 0-16 8 8 0 0 1 0 16Z"],
  ["Engaging scripts", "Hook-driven and optimised", "M5 4h9l5 5v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm9 0v5h5M8 13h8M8 17h5"],
  ["Stunning visuals", "Sourced and generated", "M4 5h16v14H4z M4 15l4.5-4.5L13 15l3-3 4 4"],
  ["Realistic narration", "Natural and engaging", "M6 10v4m4-7v10m4-13v16m4-11v6"],
] as const;

const STAGES = [
  ["planning", "Researching"],
  ["script", "Writing"],
  ["image_source", "Visuals"],
  ["audio_source", "Narration"],
  ["process", "Processing"],
  ["render_sections", "Rendering"],
  ["assemble", "Assembling"],
  ["thumbnail", "Thumbnail"],
  ["final_review", "Review"],
] as const;

export function CreateVideo({ activeJob }: { activeJob: JobRow | null }) {
  const router = useRouter();
  const [engine, setEngine] = useState(DEFAULT_ENGINE);
  const [style, setStyle] = useState(defaultStyle(DEFAULT_ENGINE));
  const [language, setLanguage] = useState(defaultLanguage(DEFAULT_ENGINE));
  const [topic, setTopic] = useState(activeJob?.topic ?? "");
  const [job, setJob] = useState<JobRow | null>(activeJob);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stalled, setStalled] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastChangeRef = useRef<number>(Date.now());

  const active = engineBySlug(engine);
  const running = job?.status === "queued" || job?.status === "running";

  useEffect(() => {
    setStyle(defaultStyle(engine));
    setLanguage(defaultLanguage(engine));
  }, [engine]);

  // Poll this job only while it is in flight.
  //
  // `stalled` is the last line of defence. The server expires abandoned jobs,
  // and the worker heartbeats so a slow render is not mistaken for a dead one
  // -- but if both of those fail, this view must still stop claiming work is
  // happening. A spinner that never resolves is the worst of the three
  // outcomes, because it gives the user nothing to act on.
  useEffect(() => {
    if (!job || !running) {
      if (pollRef.current) clearInterval(pollRef.current);
      lastChangeRef.current = Date.now();
      return;
    }
    pollRef.current = setInterval(async () => {
      try {
        const res = await fetch(`/api/jobs/${job.id}`, { cache: "no-store" });
        if (!res.ok) return;
        const next = (await res.json()) as JobRow;
        // Any movement at all resets the clock.
        if (next.updated_at !== job.updated_at || next.stage !== job.stage) {
          lastChangeRef.current = Date.now();
        }
        setJob(next);
        if (next.status === "done") router.refresh();
      } catch {
        /* transient */
      }
    }, 5000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [job, running, router]);

  // Nothing has moved for a long time: say so instead of spinning.
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      if (Date.now() - lastChangeRef.current > STALL_AFTER_MS) {
        setStalled(true);
      }
    }, 15_000);
    return () => clearInterval(timer);
  }, [running]);

  const generate = useCallback(async () => {
    const value = topic.trim();
    if (!value || busy || running) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ topic: value, engine, style, language }),
      });
      const data = (await res.json()) as Record<string, unknown>;
      if (!res.ok) {
        setError(String(data.error ?? `Could not start (${res.status}).`));
        return;
      }
      setJob(data as unknown as JobRow);
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }, [topic, engine, style, language, busy, running]);

  const stageIndex = STAGES.findIndex(([key]) => key === job?.stage);
  // A finished run reads 100 even if the last update landed at 99, and a
  // queued one never shows 0% next to "In progress".
  const pct = job
    ? job.status === "done"
      ? 100
      : Math.min(99, Math.max(1, job.progress))
    : 0;
  const currentStageLabel =
    job?.status === "done"
      ? "Video ready"
      : job?.status === "error"
        ? "Stopped"
        : (STAGES[stageIndex]?.[1] ?? "Getting started");

  return (
    <>
      {error && <div className="notice notice-error">{error}</div>}

      {/* The two ways in. "From scratch" is the default and stays selected;
          "Re-Create" is a different screen, so it links rather than toggles. */}
      <div className="mode-grid">
        <div className="mode-card is-on">
          <span className="mode-icon" aria-hidden>
            <svg width="26" height="26" viewBox="0 0 24 24" fill="none">
              <path
                d="M13 2 4.5 13.5H11L10 22l8.5-11.5H12L13 2Z"
                fill="currentColor"
              />
            </svg>
          </span>
          <div>
            <h3>Create from scratch</h3>
            <p>Enter a topic and let AI research, write, narrate and create a video.</p>
          </div>
        </div>

        <Link href="/dashboard/reels" className="mode-card is-alt">
          <span className="mode-icon" aria-hidden>
            <svg width="24" height="24" viewBox="0 0 24 24" fill="none">
              <path
                d="M4 7h11a4 4 0 0 1 0 8H8m0 0 3-3m-3 3 3 3M4 4v5h5"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <div>
            <h3>Re-Create from a video</h3>
            <p>Analyse an existing video and create a new one with the same idea.</p>
          </div>
          <span className="mode-go" aria-hidden>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <path d="m9 5 7 7-7 7" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </span>
        </Link>
      </div>

      <div className="sec-head">
        <h2>Select channel</h2>
        <Link className="sec-link" href="/dashboard">
          View all
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path d="M5 12h14m-6-7 7 7-7 7" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </Link>
      </div>

      <div className="chan-grid" role="radiogroup" aria-label="Channel">
        {ENGINES.map((e) => {
          const art = CHANNEL_ART[e.slug] ?? CHANNEL_ART._default;
          return (
            <button
              key={e.slug}
              type="button"
              role="radio"
              aria-checked={engine === e.slug}
              onClick={() => setEngine(e.slug)}
              className={`chan${engine === e.slug ? " is-on" : ""}`}
              style={{ ["--chan-art" as string]: art.field }}
              disabled={running}
            >
              {engine === e.slug && (
                <span className="chan-check" aria-hidden>
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none">
                    <path d="m5 12 5 5L19 7" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </span>
              )}
              <span className="chan-icon" aria-hidden>{art.glyph}</span>
              <h3>{e.label}</h3>
              <p>{art.blurb}</p>
            </button>
          );
        })}
      </div>

      <div className="stat" style={{ padding: 24, marginBottom: 20 }}>
        <p style={{ color: "var(--sa-dim)", fontSize: 13.5, margin: "0 0 18px", lineHeight: 1.6 }}>
          {active.sub}
        </p>

        {active.styles && active.styles.length > 0 && (
          <>
            <p style={{ fontSize: 12.5, color: "var(--sa-dim)", margin: "0 0 9px", fontWeight: 560 }}>
              Story type
            </p>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit,minmax(150px,1fr))", gap: 9, marginBottom: 18 }}>
              {active.styles.map((s) => (
                <button
                  key={s.slug}
                  type="button"
                  onClick={() => setStyle(s.slug)}
                  className={style === s.slug ? "btn-primary" : "btn-ghost"}
                  style={{ textAlign: "left", padding: "10px 12px", width: "100%" }}
                >
                  <span style={{ display: "block", fontSize: 13.5, fontWeight: 560 }}>{s.label}</span>
                  <span style={{ display: "block", fontSize: 11.5, opacity: 0.75, marginTop: 3, lineHeight: 1.45 }}>
                    {s.hint}
                  </span>
                </button>
              ))}
            </div>
          </>
        )}

        {active.languages && active.languages.length > 1 && (
          <div className="opt-group">
            <p className="opt-label" id="lang-label">Script language</p>
            <div className="seg" role="radiogroup" aria-labelledby="lang-label">
              {active.languages.map((l) => (
                <button
                  key={l.code}
                  type="button"
                  role="radio"
                  aria-checked={language === l.code}
                  onClick={() => setLanguage(l.code)}
                  className={`seg-item${language === l.code ? " seg-on" : ""}`}
                  disabled={running}
                >
                  <span className="seg-native" lang={l.code}>{l.native}</span>
                  <span className="seg-sub">{l.label}</span>
                </button>
              ))}
            </div>
            <p className="opt-hint">
              Sets the narration, captions and finished video. Visuals are
              searched in both Arabic and English either way.
            </p>
          </div>
        )}

        <div className="field">
          <label htmlFor="topic">{active.inputLabel}</label>
          <input
            id="topic"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") void generate();
            }}
            placeholder={active.placeholder}
            disabled={running}
            maxLength={300}
          />
          <span className="field-count">{topic.length}/300</span>
        </div>

        <button
          className="btn-generate"
          type="button"
          onClick={() => void generate()}
          disabled={busy || running || !topic.trim()}
        >
          <svg width="19" height="19" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path
              d="m12 3 1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3ZM19 15l.8 2.2L22 18l-2.2.8L19 21l-.8-2.2L16 18l2.2-.8L19 15Z"
              fill="currentColor"
            />
          </svg>
          {running ? "Generating…" : busy ? "Starting…" : active.submitLabel}
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path d="M5 12h14m-6-7 7 7-7 7" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>

      {/* What the pipeline guarantees, stated once, under the action it
          applies to. */}
      <div className="assure">
        {ASSURANCES.map(([title, sub, d]) => (
          <div className="assure-item" key={title}>
            <span className="assure-icon" aria-hidden>
              <svg width="19" height="19" viewBox="0 0 24 24" fill="none">
                <path d={d} stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
            <div>
              <h4>{title}</h4>
              <p>{sub}</p>
            </div>
          </div>
        ))}
      </div>

      {job && (
        <div className={`progress-card${job.status === "done" ? " is-done" : ""}${job.status === "error" ? " is-error" : ""}`}>
          {/* The number is the headline. A generation runs for minutes, so the
              one thing someone checks back for is how far along it is. */}
          <div className="progress-top">
            <div className="progress-ring" style={{ ["--pct" as string]: `${pct}` }}>
              <span>{pct}<i>%</i></span>
            </div>
            <div className="progress-meta">
              <span className={`chip ${job.status === "done" ? "chip-ok" : job.status === "error" ? "chip-err" : "chip-run"}`}>
                {job.status === "done" ? "Completed" : job.status === "error" ? "Failed" : "In progress"}
              </span>
              <h3>{currentStageLabel}</h3>
              <p>{job.error ?? job.message}</p>
            </div>
          </div>

          <div className="bar" style={{ width: "100%", margin: "18px 0 16px" }}>
            <span style={{ width: `${Math.max(2, pct)}%` }} />
          </div>

          {stalled && job.status !== "done" && job.status !== "error" && (
            <div className="notice notice-error">
              This generation has not reported progress for a while, so it has
              most likely stopped. You can start a new one.
              <button
                className="btn-ghost"
                type="button"
                style={{ width: "auto", marginTop: 10 }}
                onClick={() => {
                  setJob(null);
                  setStalled(false);
                  router.refresh();
                }}
              >
                Start over
              </button>
            </div>
          )}
          {/* Every stage, so the shape of the run is visible: what is done,
              what is happening, what is still to come. */}
          <ol className="steps">
            {STAGES.map(([key, label], i) => {
              const done = job.status === "done" || (stageIndex >= 0 && i < stageIndex);
              const now = i === stageIndex && job.status !== "done";
              return (
                <li
                  key={key}
                  className={`step${done ? " is-done" : ""}${now ? " is-now" : ""}`}
                >
                  <span className="step-dot" aria-hidden>
                    {done ? (
                      <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
                        <path d="m5 12 5 5L19 7" stroke="currentColor" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
                      </svg>
                    ) : (
                      <i />
                    )}
                  </span>
                  {label}
                </li>
              );
            })}
          </ol>
        </div>
      )}
    </>
  );
}
