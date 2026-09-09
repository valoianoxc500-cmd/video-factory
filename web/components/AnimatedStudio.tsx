"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { engineBySlug } from "@/lib/engines";
import type { JobRow } from "@/lib/repositories";
import { CUSTOMER_PROGRESS_STATES, customerProgressState, customerSafeError } from "@/lib/customer-errors";

/**
 * The Animated Stories control panel.
 *
 * Its own component rather than a mode of CreateVideo: this path has controls
 * the others do not have (scene length, character style, music) and CreateVideo
 * must keep working exactly as it does for Football, Horror and True Stories.
 */

const ENGINE = "animated_stories";
const STALL_AFTER_MS = 25 * 60 * 1000;

const DURATIONS = [
  { value: 1, label: "~60s", hint: "About 12 scenes" },
  { value: 2, label: "~2 min", hint: "About 24 scenes" },
  { value: 3, label: "~3 min", hint: "About 36 scenes" },
];

const MUSIC = [
  { value: "tense", label: "Tense", hint: "Low drones, rising" },
  { value: "playful", label: "Playful", hint: "Light, bouncy" },
  { value: "melancholy", label: "Melancholy", hint: "Sparse piano" },
  { value: "none", label: "None", hint: "Narration and SFX only" },
];

type SceneRow = {
  index: number;
  beat: string;
  start_time: number;
  end_time: number;
  duration: number;
  kind: string;
  generation_status: string;
};

export function AnimatedStudio({ activeJob }: { activeJob: JobRow | null }) {
  const router = useRouter();
  const engine = engineBySlug(ENGINE);

  const [topic, setTopic] = useState(activeJob?.topic ?? "");
  const [minutes, setMinutes] = useState(1);
  const [language, setLanguage] = useState("en");
  const [captionLanguage, setCaptionLanguage] = useState("en");
  const [sceneSeconds, setSceneSeconds] = useState(5);
  const [character, setCharacter] = useState("");
  const [music, setMusic] = useState("tense");
  const [job, setJob] = useState<JobRow | null>(activeJob);
  const [scenes, setScenes] = useState<SceneRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [stalled, setStalled] = useState(false);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastChangeRef = useRef<number>(Date.now());

  const running = job?.status === "queued" || job?.status === "running";

  // Cost is shown before anything is spent. The ceiling is the channel's, and
  // beats that do not fit are drawn but not animated -- so the number below
  // is what the run will cost, not a best case.
  const estScenes = Math.max(1, Math.round((minutes * 60) / sceneSeconds));
  const clipCost = 0.15;
  const stillCost = 0.009;
  const ceiling = 0.6;
  const animated = Math.min(estScenes, Math.floor(ceiling / clipCost));
  const estimate = (
    animated * clipCost +
    estScenes * stillCost +
    stillCost + // character sheet
    0.039 + // thumbnail
    0.12 // script, narration, reviews
  );

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
        const next = (await res.json()) as JobRow & { scenes?: SceneRow[] };
        if (next.updated_at !== job.updated_at || next.stage !== job.stage) {
          lastChangeRef.current = Date.now();
        }
        setJob(next);
        if (Array.isArray(next.scenes)) setScenes(next.scenes);
        if (next.status === "done") router.refresh();
      } catch {
        /* transient */
      }
    }, 5000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [job, running, router]);

  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => {
      if (Date.now() - lastChangeRef.current > STALL_AFTER_MS) setStalled(true);
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
          engine: ENGINE,
          style: "stick_figure",
          language,
          captionLanguage,
          // Reach the pipeline as plan/config overrides; unknown keys are
          // ignored by the existing job route rather than failing it.
          minutes,
          sceneSeconds,
          character: character.trim(),
          music,
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
  }, [topic, minutes, language, captionLanguage, sceneSeconds, character, music, busy, running]);

  const currentState = customerProgressState(job?.status, job?.stage);
  const stageIndex = CUSTOMER_PROGRESS_STATES.indexOf(
    currentState as (typeof CUSTOMER_PROGRESS_STATES)[number],
  );
  const pct = job
    ? job.status === "done" ? 100 : Math.min(99, Math.max(1, job.progress))
    : 0;

  return (
    <>
      {error && <div className="notice notice-error">{error}</div>}

      <div className="studio">
        <p className="studio-sub">{engine.sub}</p>
        {engine.voiceNote && (
          <p className="studio-voice">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path d="M4 5h16v14H4z M9 9l6 3-6 3V9Z" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" />
            </svg>
            {engine.voiceNote}
          </p>
        )}

        <div className="field">
          <label htmlFor="topic">{engine.inputLabel}</label>
          <input
            id="topic"
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") void generate(); }}
            placeholder={engine.placeholder}
            disabled={running}
            maxLength={300}
          />
          <span className="field-count">{topic.length}/300</span>
        </div>

        <div className="field">
          <label htmlFor="character">Main character</label>
          <input
            id="character"
            value={character}
            onChange={(e) => setCharacter(e.target.value)}
            placeholder="e.g. a tired night guard in a navy uniform and red cap"
            disabled={running}
            maxLength={200}
          />
          <p className="opt-hint">
            Drawn once as a reference sheet, then matched in every scene. Leave
            blank and the story picks one.
          </p>
        </div>

        <div className="opt-group">
          <p className="opt-label">Story length</p>
          <div className="seg" role="radiogroup" aria-label="Story length">
            {DURATIONS.map((d) => (
              <button key={d.value} type="button" role="radio"
                aria-checked={minutes === d.value}
                onClick={() => setMinutes(d.value)}
                className={`seg-item${minutes === d.value ? " seg-on" : ""}`}
                disabled={running}>
                <span className="seg-native">{d.label}</span>
                <span className="seg-sub">{d.hint}</span>
              </button>
            ))}
          </div>
        </div>

        <div className="opt-group">
          <p className="opt-label">Scene length</p>
          <div className="seg" role="radiogroup" aria-label="Scene length">
            {(engine.sceneDurations ?? [3, 4, 5, 6]).map((s) => (
              <button key={s} type="button" role="radio"
                aria-checked={sceneSeconds === s}
                onClick={() => setSceneSeconds(s)}
                className={`seg-item${sceneSeconds === s ? " seg-on" : ""}`}
                disabled={running}>
                <span className="seg-native">{s}s</span>
                <span className="seg-sub">per scene</span>
              </button>
            ))}
          </div>
          <p className="opt-hint">
            Animated clips run 3–6s. Longer scenes mean fewer, richer beats.
          </p>
        </div>

        <div className="lang-row">
          <div className="opt-group">
            <p className="opt-label">Voice language</p>
            <div className="seg" role="radiogroup" aria-label="Voice language">
              {(engine.voiceLanguages ?? []).map((l) => (
                <button key={l.code} type="button" role="radio"
                  aria-checked={language === l.code}
                  onClick={() => setLanguage(l.code)}
                  className={`seg-item${language === l.code ? " seg-on" : ""}`}
                  disabled={running}>
                  <span className="seg-native" lang={l.code}>{l.native}</span>
                  <span className="seg-sub">{l.label}</span>
                </button>
              ))}
            </div>
          </div>
          <div className="opt-group">
            <p className="opt-label">Caption language</p>
            <div className="seg" role="radiogroup" aria-label="Caption language">
              {(engine.captionLanguages ?? []).map((l) => (
                <button key={l.code} type="button" role="radio"
                  aria-checked={captionLanguage === l.code}
                  onClick={() => setCaptionLanguage(l.code)}
                  className={`seg-item${captionLanguage === l.code ? " seg-on" : ""}`}
                  disabled={running}>
                  <span className="seg-native" lang={l.code}>{l.native}</span>
                  <span className="seg-sub">{l.label}</span>
                </button>
              ))}
            </div>
          </div>
        </div>

        <div className="opt-group">
          <p className="opt-label">Music</p>
          <div className="style-grid" role="radiogroup" aria-label="Music">
            {MUSIC.map((m) => (
              <button key={m.value} type="button" role="radio"
                aria-checked={music === m.value}
                onClick={() => setMusic(m.value)}
                className={`style-card${music === m.value ? " is-on" : ""}`}
                disabled={running}>
                <span className="style-name">{m.label}</span>
                <span className="style-hint">{m.hint}</span>
              </button>
            ))}
          </div>
        </div>

        {/* Cost, before anything is spent. */}
        <div className="cost-panel">
          <div className="cost-row">
            <span>Estimated scenes</span><strong>{estScenes}</strong>
          </div>
          <div className="cost-row">
            <span>Animated clips (budget ${ceiling.toFixed(2)})</span>
            <strong>{animated} of {estScenes}</strong>
          </div>
          <div className="cost-row cost-total">
            <span>Estimated total</span><strong>${estimate.toFixed(2)}</strong>
          </div>
          <p className="opt-hint">
            Clips cost ${clipCost.toFixed(2)} each. Beats beyond the budget are
            still drawn — they hold their scene artwork instead of animating.
          </p>
        </div>

        <button className="btn-generate" type="button"
          onClick={() => void generate()}
          disabled={busy || running || !topic.trim()}>
          {running ? "Generating…" : busy ? "Starting…" : engine.submitLabel}
        </button>
      </div>

      {job && (
        <div className={`progress-card${job.status === "done" ? " is-done" : ""}${job.status === "error" ? " is-error" : ""}`}>
          <div className="progress-top">
            <div className="progress-ring" style={{ ["--pct" as string]: `${pct}` }}>
              <span>{pct}<i>%</i></span>
            </div>
            <div className="progress-meta">
              <span className={`chip ${job.status === "done" ? "chip-ok" : job.status === "error" ? "chip-err" : "chip-run"}`}>
                {job.status === "done" ? "Complete" : job.status === "error" ? "Needs attention" : currentState}
              </span>
              <h3>{currentState}</h3>
              <p>{job.status === "error" ? customerSafeError(job.error) : job.message}</p>
            </div>
          </div>

          <div className="bar" style={{ width: "100%", margin: "18px 0 16px" }}>
            <span style={{ width: `${Math.max(2, pct)}%` }} />
          </div>

          {stalled && job.status !== "done" && job.status !== "error" && (
            <div className="notice notice-error">
              This story has not reported progress for a while. You can start a
              new one.
            </div>
          )}

          <ol className="steps">
            {CUSTOMER_PROGRESS_STATES.map((label, i) => {
              const done = job.status === "done" || (stageIndex >= 0 && i < stageIndex);
              const now = i === stageIndex && job.status !== "done";
              return (
                <li key={label} className={`step${done ? " is-done" : ""}${now ? " is-now" : ""}`}>
                  <span className="step-dot" aria-hidden><i /></span>
                  {label}
                </li>
              );
            })}
          </ol>

          {scenes.length > 0 && (
            <div className="scene-list">
              <p className="opt-label" style={{ marginTop: 18 }}>Scenes</p>
              {scenes.map((s) => (
                <div className="scene-row" key={s.index}>
                  <span className="scene-idx">{s.index}</span>
                  <span className="scene-beat">{s.beat}</span>
                  <span className="scene-time">
                    {s.start_time.toFixed(1)}–{s.end_time.toFixed(1)}s
                  </span>
                  <span className={`chip ${
                    s.generation_status === "done" ? "chip-ok"
                      : s.generation_status === "failed" ? "chip-err" : "chip-run"
                  }`}>
                    {s.kind === "clip" ? "animated" : "still"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  );
}
