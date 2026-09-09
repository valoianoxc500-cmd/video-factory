"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import {
  DEFAULT_ENGINE,
  defaultCaptionLanguage,
  defaultLanguage,
  defaultStyle,
  engineBySlug,
} from "@/lib/engines";
import type { JobRow } from "@/lib/repositories";
import { CUSTOMER_PROGRESS_STATES, customerProgressState, customerSafeError } from "@/lib/customer-errors";

/**
 * The generation workflow for one engine.
 *
 * Deliberately the same shape the pipeline already expects -- engine slug,
 * topic, and the story type -- so nothing about how a video is produced
 * changes. What is new is that voice and caption language are chosen
 * separately, and that each engine has its own screen rather than a channel
 * picker: you arrive from the sidebar already knowing what you came to make.
 */

/**
 * How long the view will show progress with nothing changing.
 *
 * Above the worker's heartbeat and the server's expiry window, so this only
 * fires when both of those have already failed.
 */
const STALL_AFTER_MS = 18 * 60 * 1000;

/** The four things the pipeline promises, in the order it does them. */
const ASSURANCES = [
  ["Verified sources", "Real information only", "M21 21l-4.3-4.3M11 19a8 8 0 1 1 0-16 8 8 0 0 1 0 16Z"],
  ["Engaging scripts", "Hook-driven and optimised", "M5 4h9l5 5v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm9 0v5h5M8 13h8M8 17h5"],
  ["Stunning visuals", "Sourced and generated", "M4 5h16v14H4z M4 15l4.5-4.5L13 15l3-3 4 4"],
  ["Realistic narration", "Natural and engaging", "M6 10v4m4-7v10m4-13v16m4-11v6"],
] as const;

export function CreateVideo({
  activeJob,
  engine: lockedEngine,
}: {
  activeJob: JobRow | null;
  /** The engine this screen makes. Omitted only by the legacy /create route. */
  engine?: string;
}) {
  const router = useRouter();
  const engine = lockedEngine ?? DEFAULT_ENGINE;
  const [style, setStyle] = useState(defaultStyle(engine));
  const [language, setLanguage] = useState(defaultLanguage(engine));
  const [captionLanguage, setCaptionLanguage] = useState(
    defaultCaptionLanguage(engine),
  );
  const [topic, setTopic] = useState(activeJob?.topic ?? "");
  const [job, setJob] = useState<JobRow | null>(activeJob);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stalled, setStalled] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastChangeRef = useRef<number>(Date.now());

  const active = engineBySlug(engine);
  const running = job?.status === "queued" || job?.status === "running";
  const translated = Boolean(
    captionLanguage && language && captionLanguage !== language,
  );

  useEffect(() => {
    setStyle(defaultStyle(engine));
    setLanguage(defaultLanguage(engine));
    setCaptionLanguage(defaultCaptionLanguage(engine));
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
        body: JSON.stringify({
          topic: value,
          engine,
          style,
          language,
          captionLanguage,
        }),
      });
      const data = (await res.json()) as Record<string, unknown>;
      if (!res.ok) {
        setError(customerSafeError(data.error ?? "Could not start this generation."));
        return;
      }
      setJob(data as unknown as JobRow);
    } catch {
      setError("We could not start this generation right now. Please try again.");
    } finally {
      setBusy(false);
    }
  }, [topic, engine, style, language, captionLanguage, busy, running]);

  const currentState = customerProgressState(job?.status, job?.stage);
  const stageIndex = CUSTOMER_PROGRESS_STATES.indexOf(
    currentState as (typeof CUSTOMER_PROGRESS_STATES)[number],
  );
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
        ? "Needs attention"
        : currentState;

  return (
    <>
      {error && <div className="notice notice-error">{error}</div>}

      <div className="studio">
        <p className="studio-sub">{active.sub}</p>
        {active.voiceNote && (
          <p className="studio-voice">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path
                d="M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3Zm7 9a7 7 0 0 1-14 0m7 7v3"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            {active.voiceNote}
          </p>
        )}

        {active.styles && active.styles.length > 1 && (
          <div className="opt-group">
            <p className="opt-label" id="style-label">
              Story type
            </p>
            <div className="style-grid" role="radiogroup" aria-labelledby="style-label">
              {active.styles.map((s) => (
                <button
                  key={s.slug}
                  type="button"
                  role="radio"
                  aria-checked={style === s.slug}
                  onClick={() => setStyle(s.slug)}
                  className={`style-card${style === s.slug ? " is-on" : ""}`}
                  disabled={running}
                >
                  <span className="style-name">{s.label}</span>
                  <span className="style-hint">{s.hint}</span>
                </button>
              ))}
            </div>
          </div>
        )}

        {/* Voice and captions are two choices, not one, so they are two
            controls. The hint below them states exactly what the pipeline
            guarantees when they differ -- sentence-level sync -- rather than
            implying word-level sync it cannot measure across a translation. */}
        <div className="lang-row">
          {active.voiceLanguages && active.voiceLanguages.length > 0 && (
            <div className="opt-group">
              <p className="opt-label" id="voice-lang-label">
                Voice language
              </p>
              <div className="seg" role="radiogroup" aria-labelledby="voice-lang-label">
                {active.voiceLanguages.map((l) => (
                  <button
                    key={l.code}
                    type="button"
                    role="radio"
                    aria-checked={language === l.code}
                    onClick={() => setLanguage(l.code)}
                    className={`seg-item${language === l.code ? " seg-on" : ""}`}
                    disabled={running || active.voiceLanguages!.length < 2}
                  >
                    <span className="seg-native" lang={l.code}>
                      {l.native}
                    </span>
                    <span className="seg-sub">{l.label}</span>
                  </button>
                ))}
              </div>
              <p className="opt-hint">
                {active.voiceLanguages.length < 2
                  ? "This channel narrates in Arabic only."
                  : "What the narrator speaks."}
              </p>
            </div>
          )}

          {active.captionLanguages && active.captionLanguages.length > 0 && (
            <div className="opt-group">
              <p className="opt-label" id="caption-lang-label">
                Caption language
              </p>
              <div className="seg" role="radiogroup" aria-labelledby="caption-lang-label">
                {active.captionLanguages.map((l) => (
                  <button
                    key={l.code}
                    type="button"
                    role="radio"
                    aria-checked={captionLanguage === l.code}
                    onClick={() => setCaptionLanguage(l.code)}
                    className={`seg-item${captionLanguage === l.code ? " seg-on" : ""}`}
                    disabled={running}
                  >
                    <span className="seg-native" lang={l.code}>
                      {l.native}
                    </span>
                    <span className="seg-sub">{l.label}</span>
                  </button>
                ))}
              </div>
              <p className="opt-hint">
                {translated
                  ? "Captions are translated and locked to the narration’s measured sentence timings, so each line appears and leaves exactly when it is spoken."
                  : "Captions are taken from the narration audio itself — every word timed to the moment it is said."}
              </p>
            </div>
          )}
        </div>

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

        {active.examples.length > 0 && !running && (
          <div className="examples">
            <span className="examples-label">Try</span>
            {active.examples.map((ex) => (
              <button
                key={ex}
                type="button"
                className="example-chip"
                onClick={() => setTopic(ex)}
              >
                {ex}
              </button>
            ))}
          </div>
        )}

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
                {job.status === "done" ? "Complete" : job.status === "error" ? "Needs attention" : currentState}
              </span>
              <h3>{currentStageLabel}</h3>
              <p>{job.status === "error" ? customerSafeError(job.error) : job.message}</p>
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
          <ol className="steps">
            {CUSTOMER_PROGRESS_STATES.map((label, i) => {
              const done = job.status === "done" || (stageIndex >= 0 && i < stageIndex);
              const now = i === stageIndex && job.status !== "done";
              return (
                <li
                  key={label}
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
