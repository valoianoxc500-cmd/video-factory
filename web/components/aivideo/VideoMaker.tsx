"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ASPECT_RATIOS,
  CAPTION_PRESETS,
  DEFAULT_SETTINGS,
  DURATIONS,
  FONTS,
  applyLanguage,
  progressLabel,
  voicesFor,
  type Language,
  type VideoJob,
  type VideoSettings,
} from "@/lib/aivideo";

/**
 * AI Video Maker.
 *
 * The default screen is a topic box and five choices. Everything else lives
 * behind Advanced, because the customer who wants a video about Dubai should
 * not have to have an opinion about outline width before they can get one.
 *
 * The caption preview and the voice samples are the two places this differs
 * from a settings form: both show the actual thing that will end up in the
 * video, so a choice can be made by looking rather than by guessing.
 */

const STEPS = [
  { id: "script", label: "Writing script" },
  { id: "footage", label: "Finding footage" },
  { id: "voice", label: "Creating voice" },
  { id: "captions", label: "Adding captions" },
  { id: "render", label: "Rendering" },
  { id: "finalize", label: "Finalizing" },
];

const SEEDS: Record<Language, string[]> = {
  en: [
    "5 crazy facts about Dubai",
    "Why the Titanic sank",
    "How coffee changed the world",
    "The deepest place on Earth",
  ],
  ar: [
    "خمس حقائق مذهلة عن دبي",
    "لماذا غرقت سفينة تيتانيك",
    "كيف غيّرت القهوة العالم",
    "أعمق مكان على وجه الأرض",
  ],
};

const CAPTION_SAMPLE: Record<Language, string> = {
  en: "THIS CHANGED EVERYTHING",
  ar: "هذا غيّر كل شيء",
};

// The preset tiles are ~112px wide, so they show a short phrase rather than
// the full sample: at tile scale the long one wrapped to four cramped lines
// and every preset looked alike.
const PRESET_SAMPLE: Record<Language, string> = {
  en: "YOUR CAPTIONS",
  ar: "نص الفيديو",
};

function fontStack(id: string) {
  return FONTS.find((f) => f.id === id)?.stack ?? "system-ui, sans-serif";
}

function initials(label: string) {
  return label.trim().slice(0, 1).toUpperCase();
}

/** A caption rendered the way the burned-in track will look. */
function CaptionSample({
  settings,
  scale = 1,
  text,
}: {
  settings: VideoSettings;
  scale?: number;
  text?: string;
}) {
  const c = settings.captions;
  const body = text ?? CAPTION_SAMPLE[settings.language];
  const shown = c.uppercase ? body.toUpperCase() : body;
  const size = Math.max(9, c.size * scale);
  const outline = Math.max(0, c.outline_width * scale);
  // Four offset shadows approximate libass's outline well enough to judge it.
  const stroke = outline
    ? [
        `${outline}px 0 0 ${c.outline_color}`,
        `-${outline}px 0 0 ${c.outline_color}`,
        `0 ${outline}px 0 ${c.outline_color}`,
        `0 -${outline}px 0 ${c.outline_color}`,
      ].join(", ")
    : "none";

  return (
    <span
      className="avm-preset-sample"
      dir={settings.language === "ar" ? "rtl" : "ltr"}
      style={{
        color: c.text_color,
        fontFamily: fontStack(c.font),
        fontSize: `${size}px`,
        textShadow: stroke,
        background: c.background !== "none" ? c.background : undefined,
        padding: c.background !== "none" ? `${4 * scale}px ${8 * scale}px` : undefined,
        borderRadius: c.background !== "none" ? `${5 * scale}px` : undefined,
      }}
    >
      {shown}
    </span>
  );
}

export function VideoMaker() {
  const [topic, setTopic] = useState("");
  const [settings, setSettings] = useState<VideoSettings>(DEFAULT_SETTINGS);
  const [jobs, setJobs] = useState<VideoJob[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [loaded, setLoaded] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const active = useMemo(
    () => jobs.find((j) => j.id === activeId) ?? null,
    [jobs, activeId],
  );
  const latestDone = useMemo(() => jobs.find((j) => j.status === "done"), [jobs]);
  const preview = active?.status === "done" ? active : active ?? latestDone;
  const voices = voicesFor(settings.language);

  const patch = useCallback((next: Partial<VideoSettings>) => {
    setSettings((s) => ({ ...s, ...next }));
  }, []);
  const patchCaptions = useCallback((next: Partial<VideoSettings["captions"]>) => {
    setSettings((s) => ({ ...s, captions: { ...s.captions, ...next } }));
  }, []);

  // Restore the customer's saved setup, then their recent work.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch("/api/aivideo/prefs");
        const data = await res.json();
        if (!cancelled && data?.settings) setSettings(data.settings);
      } catch {
        // Defaults are already in state; a failed read is not worth a message.
      } finally {
        if (!cancelled) setLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch("/api/aivideo/jobs");
      const data = await res.json();
      if (Array.isArray(data?.jobs)) setJobs(data.jobs);
    } catch {
      // Transient. The next poll will pick it up.
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Poll only while something is actually generating.
  useEffect(() => {
    const working = jobs.some((j) => j.status === "queued" || j.status === "running");
    if (!working) return;
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [jobs, refresh]);

  function changeLanguage(language: Language) {
    setSettings((s) => applyLanguage(s, language));
  }

  function playVoice(id: string) {
    audioRef.current?.pause();
    const audio = new Audio(`/voice-previews/${id}.mp3`);
    audioRef.current = audio;
    void audio.play().catch(() => setNote({ kind: "error", text: "Could not play that sample." }));
  }

  async function generate() {
    if (topic.trim().length < 3) {
      setNote({ kind: "error", text: "Tell us what the video should be about." });
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const res = await fetch("/api/aivideo/jobs", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ topic: topic.trim(), settings }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data?.error ?? "Could not start that video.");
      setActiveId(data.id);
      setNote({ kind: "ok", text: "Your video is generating. You can leave this page." });
      await refresh();
    } catch (err) {
      setNote({ kind: "error", text: (err as Error).message });
    } finally {
      setBusy(false);
    }
  }

  async function saveDefaults() {
    try {
      const res = await fetch("/api/aivideo/prefs", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ settings }),
      });
      if (!res.ok) throw new Error("Could not save your defaults.");
      setNote({ kind: "ok", text: "Saved. New videos will start with this setup." });
    } catch (err) {
      setNote({ kind: "error", text: (err as Error).message });
    }
  }

  const stepState = (id: string) => {
    if (!active || active.status === "done") return "idle";
    const order = STEPS.map((s) => s.id);
    const current = order.indexOf(active.stage);
    const mine = order.indexOf(id);
    if (current < 0) return "idle";
    if (mine < current) return "done";
    return mine === current ? "active" : "idle";
  };

  return (
    <div className="avm">
      {/* ── composer ─────────────────────────────────────────── */}
      <div>
        <section className="avm-compose">
          <label className="avm-label" htmlFor="avm-topic">
            What should the video be about?
          </label>
          <textarea
            id="avm-topic"
            className="avm-topic"
            dir={settings.language === "ar" ? "rtl" : "ltr"}
            placeholder={
              settings.language === "ar"
                ? "اكتب فكرتك… مثال: خمس حقائق مذهلة عن دبي"
                : "Write your idea… e.g. 5 crazy facts about Dubai"
            }
            maxLength={400}
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
          />
          <div className="avm-seeds">
            {SEEDS[settings.language].map((seed) => (
              <button key={seed} type="button" className="avm-seed" onClick={() => setTopic(seed)}>
                {seed}
              </button>
            ))}
          </div>

          <div className="avm-grid">
            <div>
              <span className="avm-label">Language</span>
              <div className="avm-seg">
                <button
                  type="button"
                  aria-pressed={settings.language === "en"}
                  onClick={() => changeLanguage("en")}
                >
                  English
                </button>
                <button
                  type="button"
                  aria-pressed={settings.language === "ar"}
                  onClick={() => changeLanguage("ar")}
                >
                  العربية
                </button>
              </div>
            </div>

            <div>
              <span className="avm-label">Duration</span>
              <div className="avm-seg">
                {DURATIONS.map((d) => (
                  <button
                    key={d}
                    type="button"
                    aria-pressed={settings.duration_seconds === d}
                    onClick={() => patch({ duration_seconds: d })}
                  >
                    {d}s
                  </button>
                ))}
              </div>
            </div>

            <div>
              <span className="avm-label">Format</span>
              <div className="avm-seg">
                {ASPECT_RATIOS.map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    aria-pressed={settings.aspect_ratio === a.id}
                    onClick={() => patch({ aspect_ratio: a.id })}
                  >
                    {a.label}
                    <small>{a.hint}</small>
                  </button>
                ))}
              </div>
            </div>
          </div>

          <div className="avm-grid">
            <div style={{ gridColumn: "1 / -1" }}>
              <span className="avm-label">Voice</span>
              <div className="avm-voices">
                {voices.map((v) => (
                  <div
                    key={v.id}
                    role="button"
                    tabIndex={0}
                    aria-pressed={settings.voice === v.id}
                    className="avm-voice"
                    onClick={() => patch({ voice: v.id })}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        patch({ voice: v.id });
                      }
                    }}
                  >
                    <span
                      className="avm-voice-dot"
                      style={{
                        background:
                          v.gender === "female"
                            ? "linear-gradient(140deg,#FF9BC4,#7A5CFF)"
                            : "linear-gradient(140deg,#4FD1FF,#14E08C)",
                      }}
                    >
                      {initials(v.label)}
                    </span>
                    <span className="avm-voice-body">
                      <span className="avm-voice-name">{v.label}</span>
                      <span className="avm-voice-note">{v.note}</span>
                    </span>
                    <button
                      type="button"
                      className="avm-play"
                      title={`Preview ${v.label}`}
                      onClick={(e) => {
                        e.stopPropagation();
                        playVoice(v.id);
                      }}
                    >
                      ▶
                    </button>
                  </div>
                ))}
              </div>
            </div>
          </div>

          <div style={{ marginTop: 22 }}>
            <span className="avm-label">Caption style</span>
            <div className="avm-presets">
              {Object.entries(CAPTION_PRESETS).map(([id, p]) => {
                const asSettings: VideoSettings = {
                  ...settings,
                  captions: { ...p, preset: id },
                };
                return (
                  <button
                    key={id}
                    type="button"
                    className="avm-preset"
                    aria-pressed={settings.captions.preset === id}
                    onClick={() =>
                      patchCaptions(
                        applyLanguage({ ...settings, captions: { ...p, preset: id } }, settings.language)
                          .captions,
                      )
                    }
                  >
                    <CaptionSample
                      settings={asSettings}
                      scale={0.2}
                      text={PRESET_SAMPLE[settings.language]}
                    />
                    <span className="avm-preset-name">{p.label}</span>
                  </button>
                );
              })}
            </div>
          </div>

          {/* ── advanced ───────────────────────────────────────── */}
          <details className="avm-adv">
            <summary>Advanced settings</summary>
            <div className="avm-adv-body">
              <div className="avm-grid" style={{ marginTop: 0 }}>
                <div>
                  <span className="avm-label">Caption font</span>
                  <select
                    className="avm-select"
                    value={settings.captions.font}
                    onChange={(e) => patchCaptions({ font: e.target.value })}
                  >
                    {FONTS.filter((f) => settings.language !== "ar" || f.arabic).map((f) => (
                      <option key={f.id} value={f.id}>
                        {f.label}
                        {f.arabic ? " · Arabic" : ""}
                      </option>
                    ))}
                  </select>
                </div>
                <div>
                  <span className="avm-label">Caption position</span>
                  <div className="avm-seg">
                    {(["top", "center", "bottom"] as const).map((p) => (
                      <button
                        key={p}
                        type="button"
                        aria-pressed={settings.captions.position === p}
                        onClick={() => patchCaptions({ position: p })}
                      >
                        {p}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <span className="avm-label">Music</span>
                  <select
                    className="avm-select"
                    value={settings.music}
                    onChange={(e) => patch({ music: e.target.value })}
                  >
                    <option value="auto">Auto</option>
                    <option value="none">None</option>
                  </select>
                </div>
              </div>

              <div className="avm-slider">
                <header>
                  <span>Caption size</span>
                  <b>{settings.captions.size}px</b>
                </header>
                <input
                  type="range"
                  min={24}
                  max={140}
                  value={settings.captions.size}
                  onChange={(e) => patchCaptions({ size: Number(e.target.value) })}
                />
              </div>

              <div className="avm-slider">
                <header>
                  <span>Outline width</span>
                  <b>{settings.captions.outline_width}px</b>
                </header>
                <input
                  type="range"
                  min={0}
                  max={16}
                  value={settings.captions.outline_width}
                  onChange={(e) => patchCaptions({ outline_width: Number(e.target.value) })}
                />
              </div>

              <div className="avm-slider">
                <header>
                  <span>Words per line</span>
                  <b>{settings.captions.words_per_line}</b>
                </header>
                <input
                  type="range"
                  min={1}
                  max={12}
                  value={settings.captions.words_per_line}
                  onChange={(e) => patchCaptions({ words_per_line: Number(e.target.value) })}
                />
              </div>

              <div className="avm-colors">
                <label className="avm-color">
                  <span>Text</span>
                  <input
                    type="color"
                    value={settings.captions.text_color}
                    onChange={(e) => patchCaptions({ text_color: e.target.value })}
                  />
                </label>
                <label className="avm-color">
                  <span>Highlight</span>
                  <input
                    type="color"
                    value={settings.captions.highlight_color}
                    onChange={(e) => patchCaptions({ highlight_color: e.target.value })}
                  />
                </label>
                <label className="avm-color">
                  <span>Outline</span>
                  <input
                    type="color"
                    value={settings.captions.outline_color}
                    onChange={(e) => patchCaptions({ outline_color: e.target.value })}
                  />
                </label>
              </div>

              <div className="avm-slider">
                <header>
                  <span>Voice speed</span>
                  <b>{settings.voice_rate.toFixed(2)}×</b>
                </header>
                <input
                  type="range"
                  min={0.5}
                  max={2}
                  step={0.05}
                  value={settings.voice_rate}
                  onChange={(e) => patch({ voice_rate: Number(e.target.value) })}
                />
              </div>

              <div className="avm-slider">
                <header>
                  <span>Music volume</span>
                  <b>{Math.round(settings.music_volume * 100)}%</b>
                </header>
                <input
                  type="range"
                  min={0}
                  max={0.5}
                  step={0.01}
                  value={settings.music_volume}
                  onChange={(e) => patch({ music_volume: Number(e.target.value) })}
                />
              </div>

              <div className="avm-slider">
                <header>
                  <span>Clip switching</span>
                  <b>every {settings.clip_seconds.toFixed(1)}s</b>
                </header>
                <input
                  type="range"
                  min={1.5}
                  max={10}
                  step={0.5}
                  value={settings.clip_seconds}
                  onChange={(e) => patch({ clip_seconds: Number(e.target.value) })}
                />
              </div>

              <div className="avm-grid" style={{ marginTop: 0 }}>
                <div>
                  <span className="avm-label">Fit</span>
                  <div className="avm-seg">
                    {(["cover", "contain"] as const).map((m) => (
                      <button
                        key={m}
                        type="button"
                        aria-pressed={settings.fit_mode === m}
                        onClick={() => patch({ fit_mode: m })}
                      >
                        {m}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <span className="avm-label">Quality</span>
                  <div className="avm-seg">
                    {(["high", "balanced"] as const).map((q) => (
                      <button
                        key={q}
                        type="button"
                        aria-pressed={settings.quality === q}
                        onClick={() => patch({ quality: q })}
                      >
                        {q}
                      </button>
                    ))}
                  </div>
                </div>
                <div>
                  <span className="avm-label">Captions</span>
                  <div className="avm-seg">
                    <button
                      type="button"
                      aria-pressed={settings.captions.enabled}
                      onClick={() => patchCaptions({ enabled: true })}
                    >
                      On
                    </button>
                    <button
                      type="button"
                      aria-pressed={!settings.captions.enabled}
                      onClick={() => patchCaptions({ enabled: false })}
                    >
                      Off
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </details>

          <button
            type="button"
            className="avm-generate"
            disabled={busy || !loaded}
            onClick={generate}
          >
            {busy ? "Starting…" : "Generate video"}
          </button>

          <div className="avm-savebar">
            <button type="button" className="avm-save" onClick={saveDefaults}>
              Save as default
            </button>
            <span style={{ color: "var(--sa-faint)", font: "500 12px/1 var(--sa-body)" }}>
              Free voices · licensed footage
            </span>
          </div>

          {note && (
            <div className="avm-note" data-kind={note.kind}>
              {note.text}
            </div>
          )}
        </section>
      </div>

      {/* ── rail ─────────────────────────────────────────────── */}
      <aside className="avm-rail">
        <section className="avm-panel">
          <h3>Preview</h3>
          <div className="avm-stage" data-ratio={settings.aspect_ratio}>
            {preview?.status === "done" && preview.video_url ? (
              <video src={preview.video_url} controls playsInline preload="metadata" />
            ) : (
              <>
                <span className="avm-empty">
                  {active && active.status !== "error"
                    ? progressLabel(active)
                    : "Your video will appear here"}
                </span>
                {settings.captions.enabled && (
                  <span className="avm-caption-demo" data-pos={settings.captions.position}>
                    <CaptionSample settings={settings} scale={0.42} />
                  </span>
                )}
              </>
            )}
          </div>

          {active && active.status !== "done" && active.status !== "error" && (
            <div className="avm-prog" style={{ marginTop: 16 }}>
              <div className="avm-prog-head">
                <strong>{progressLabel(active)}</strong>
                <span>{active.progress}%</span>
              </div>
              <div className="avm-bar">
                <i style={{ width: `${Math.max(4, active.progress)}%` }} />
              </div>
              <div className="avm-steps">
                {STEPS.map((s) => (
                  <span key={s.id} className="avm-step" data-state={stepState(s.id)}>
                    <i />
                    {s.label}
                  </span>
                ))}
              </div>
            </div>
          )}

          {active?.stalled && (
            <div className="avm-note" data-kind="warn">
              {active.message}
            </div>
          )}

          {active?.status === "error" && (
            <div className="avm-note" data-kind="error">
              {active.error || "That one didn't finish. Try generating again."}
            </div>
          )}

          {preview?.status === "done" && preview.video_url && (
            <div className="avm-actions">
              <a href={preview.video_url} download>
                Download
              </a>
              <button
                type="button"
                onClick={() => {
                  setTopic(preview.topic);
                  window.scrollTo({ top: 0, behavior: "smooth" });
                }}
              >
                Regenerate
              </button>
            </div>
          )}
        </section>

        <section className="avm-panel">
          <h3>Recent videos</h3>
          {jobs.length === 0 ? (
            <p className="avm-empty" style={{ padding: "8px 0" }}>
              Nothing yet. Your finished videos will collect here.
            </p>
          ) : (
            <div className="avm-recent">
              {jobs.slice(0, 8).map((job) => (
                <div
                  key={job.id}
                  className="avm-item"
                  role="button"
                  tabIndex={0}
                  onClick={() => setActiveId(job.id)}
                  onKeyDown={(e) => e.key === "Enter" && setActiveId(job.id)}
                >
                  {job.thumbnail_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img className="avm-thumb" src={job.thumbnail_url} alt="" />
                  ) : (
                    <span className="avm-thumb" />
                  )}
                  <span className="avm-item-body">
                    <span className="avm-item-topic">{job.topic}</span>
                    <span className="avm-item-meta">
                      {job.language === "ar" ? "العربية" : "English"} ·{" "}
                      {job.duration_seconds}s ·{" "}
                      {new Date(job.created_at).toLocaleDateString()}
                    </span>
                  </span>
                  <span className="avm-pill" data-s={job.status}>
                    {job.status === "done" ? "Ready" : job.status === "error" ? "Failed" : "Working"}
                  </span>
                </div>
              ))}
            </div>
          )}
        </section>
      </aside>
    </div>
  );
}
