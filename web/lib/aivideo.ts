/**
 * AI Video Maker: the shared vocabulary between the form, the API and the
 * engine.
 *
 * These constants are the TypeScript half of `aivideo/spec.py`. They are
 * duplicated rather than generated because there are about forty values and a
 * codegen step would cost more than it saves — but the Python side clamps
 * every field it receives, so a drift here degrades to a corrected value
 * rather than a failed job. `tests/aivideo-contract.test.ts` fails if the two
 * lists stop agreeing.
 */

export type Language = "en" | "ar";
export type AspectRatio = "9:16" | "16:9" | "1:1";

export const DURATIONS = [30, 45, 60, 90] as const;

export const ASPECT_RATIOS: { id: AspectRatio; label: string; hint: string }[] = [
  { id: "9:16", label: "9:16", hint: "TikTok · Reels · Shorts" },
  { id: "16:9", label: "16:9", hint: "YouTube · landscape" },
  { id: "1:1", label: "1:1", hint: "Feed square" },
];

export type Voice = {
  id: string;
  label: string;
  language: Language;
  gender: "male" | "female";
  note: string;
};

export const VOICES: Voice[] = [
  { id: "en-US-AriaNeural", label: "Aria", language: "en", gender: "female", note: "Warm, natural" },
  { id: "en-US-GuyNeural", label: "Guy", language: "en", gender: "male", note: "Confident narrator" },
  { id: "en-US-JennyNeural", label: "Jenny", language: "en", gender: "female", note: "Friendly, upbeat" },
  { id: "en-US-ChristopherNeural", label: "Christopher", language: "en", gender: "male", note: "Deep documentary" },
  { id: "en-GB-SoniaNeural", label: "Sonia", language: "en", gender: "female", note: "British" },
  { id: "en-GB-RyanNeural", label: "Ryan", language: "en", gender: "male", note: "British" },
  { id: "ar-EG-SalmaNeural", label: "سلمى", language: "ar", gender: "female", note: "مصري · واضح" },
  { id: "ar-EG-ShakirNeural", label: "شاكر", language: "ar", gender: "male", note: "مصري · إخباري" },
  { id: "ar-SA-ZariyahNeural", label: "زارية", language: "ar", gender: "female", note: "فصحى" },
  { id: "ar-SA-HamedNeural", label: "حامد", language: "ar", gender: "male", note: "فصحى · عميق" },
  { id: "ar-AE-FatimaNeural", label: "فاطمة", language: "ar", gender: "female", note: "خليجي" },
];

export function voicesFor(language: Language): Voice[] {
  return VOICES.filter((v) => v.language === language);
}

export type CaptionStyle = {
  preset: string;
  font: string;
  size: number;
  weight: string;
  text_color: string;
  highlight_color: string;
  outline_color: string;
  outline_width: number;
  background: string;
  position: "top" | "center" | "bottom";
  words_per_line: number;
  uppercase: boolean;
  enabled: boolean;
};

export const CAPTION_PRESETS: Record<string, CaptionStyle & { label: string }> = {
  clean: {
    label: "Clean", preset: "clean", font: "inter", size: 64, weight: "bold",
    text_color: "#FFFFFF", highlight_color: "#FFFFFF", outline_color: "#000000",
    outline_width: 4, background: "none", position: "bottom", words_per_line: 4,
    uppercase: false, enabled: true,
  },
  bold: {
    label: "Bold", preset: "bold", font: "montserrat", size: 76, weight: "black",
    text_color: "#FFFFFF", highlight_color: "#FFE500", outline_color: "#000000",
    outline_width: 7, background: "none", position: "center", words_per_line: 3,
    uppercase: true, enabled: true,
  },
  viral: {
    label: "Viral", preset: "viral", font: "montserrat", size: 82, weight: "black",
    text_color: "#FFFFFF", highlight_color: "#00E676", outline_color: "#000000",
    outline_width: 8, background: "none", position: "center", words_per_line: 3,
    uppercase: true, enabled: true,
  },
  minimal: {
    label: "Minimal", preset: "minimal", font: "inter", size: 52, weight: "medium",
    text_color: "#FFFFFF", highlight_color: "#FFFFFF", outline_color: "#000000",
    outline_width: 2, background: "none", position: "bottom", words_per_line: 6,
    uppercase: false, enabled: true,
  },
  boxed: {
    label: "Boxed", preset: "boxed", font: "inter", size: 60, weight: "bold",
    text_color: "#FFFFFF", highlight_color: "#FFD600", outline_color: "#000000",
    outline_width: 0, background: "#000000CC", position: "bottom", words_per_line: 5,
    uppercase: false, enabled: true,
  },
  karaoke: {
    label: "Karaoke", preset: "karaoke", font: "montserrat", size: 70, weight: "bold",
    text_color: "#FFFFFF", highlight_color: "#FF3D71", outline_color: "#000000",
    outline_width: 6, background: "none", position: "center", words_per_line: 4,
    uppercase: false, enabled: true,
  },
  cinematic: {
    label: "Cinematic", preset: "cinematic", font: "playfair", size: 56, weight: "medium",
    text_color: "#F5F0E6", highlight_color: "#E0C070", outline_color: "#000000",
    outline_width: 3, background: "none", position: "bottom", words_per_line: 5,
    uppercase: false, enabled: true,
  },
};

/** Families that can shape Arabic; mirrors ARABIC_CAPABLE_FONTS in spec.py. */
export const ARABIC_FONTS = ["cairo", "tajawal", "notoarabic", "arial", "tahoma"];

export const FONTS: { id: string; label: string; arabic: boolean; stack: string }[] = [
  { id: "inter", label: "Inter", arabic: false, stack: "'Inter', system-ui, sans-serif" },
  { id: "montserrat", label: "Montserrat", arabic: false, stack: "'Montserrat', system-ui, sans-serif" },
  { id: "playfair", label: "Playfair", arabic: false, stack: "'Playfair Display', Georgia, serif" },
  { id: "cairo", label: "Cairo", arabic: true, stack: "'Cairo', 'Segoe UI', Tahoma, sans-serif" },
  { id: "tajawal", label: "Tajawal", arabic: true, stack: "'Tajawal', 'Segoe UI', Tahoma, sans-serif" },
  { id: "notoarabic", label: "Noto Arabic", arabic: true, stack: "'Noto Naskh Arabic', Tahoma, serif" },
  { id: "arial", label: "Arial", arabic: true, stack: "Arial, Helvetica, sans-serif" },
  { id: "tahoma", label: "Tahoma", arabic: true, stack: "Tahoma, Verdana, sans-serif" },
];

export type VideoSettings = {
  language: Language;
  duration_seconds: number;
  aspect_ratio: AspectRatio;
  voice: string;
  voice_rate: number;
  voice_volume: number;
  music: string;
  music_volume: number;
  clip_seconds: number;
  transition: "none" | "fade";
  fit_mode: "cover" | "contain";
  quality: "high" | "balanced";
  captions: CaptionStyle;
};

export const DEFAULT_SETTINGS: VideoSettings = {
  language: "en",
  duration_seconds: 30,
  aspect_ratio: "9:16",
  voice: "en-US-AriaNeural",
  voice_rate: 1,
  voice_volume: 1,
  music: "auto",
  music_volume: 0.12,
  clip_seconds: 3.5,
  transition: "none",
  fit_mode: "cover",
  quality: "high",
  captions: { ...CAPTION_PRESETS.clean },
};

/**
 * Apply the consequences of a language change.
 *
 * Switching to Arabic with an English voice selected would produce English
 * phonemes reading Arabic text, and a Latin-only caption font would draw
 * disconnected letters. Both are silent failures in the finished MP4, so the
 * switch fixes them here rather than letting the customer discover them.
 */
export function applyLanguage(settings: VideoSettings, language: Language): VideoSettings {
  const voices = voicesFor(language);
  const keepVoice = voices.some((v) => v.id === settings.voice);
  const captions = { ...settings.captions };
  if (language === "ar") {
    if (!ARABIC_FONTS.includes(captions.font)) captions.font = "cairo";
    captions.uppercase = false;
  }
  return {
    ...settings,
    language,
    voice: keepVoice ? settings.voice : voices[0]?.id ?? settings.voice,
    captions,
  };
}

export type JobStatus = "queued" | "running" | "done" | "error";

export type VideoJob = {
  id: string;
  topic: string;
  language: Language;
  duration_seconds: number;
  aspect_ratio: AspectRatio;
  status: JobStatus;
  stage: string;
  progress: number;
  message: string;
  error: string;
  video_url: string;
  thumbnail_url: string;
  duration_actual: number;
  cost_usd: number;
  created_at: string;
};

/** The only progress wording the customer ever sees. */
export const PROGRESS_LABELS: Record<string, string> = {
  "": "Preparing your video",
  script: "Writing script",
  footage: "Finding footage",
  voice: "Creating voice",
  captions: "Adding captions",
  render: "Rendering",
  finalize: "Finalizing",
};

export function progressLabel(job: Pick<VideoJob, "status" | "stage" | "message">): string {
  if (job.status === "done") return "Ready";
  if (job.status === "error") return "Needs another try";
  if (job.status === "queued") return "Waiting to start";
  return job.message || PROGRESS_LABELS[job.stage] || "Working on your video";
}

export function sanitiseSettings(raw: unknown): VideoSettings {
  const input = (raw ?? {}) as Partial<VideoSettings>;
  const language: Language = input.language === "ar" ? "ar" : "en";
  const clamp = (v: unknown, lo: number, hi: number, dflt: number) => {
    const n = typeof v === "number" ? v : Number(v);
    return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : dflt;
  };
  const presetName = String(input.captions?.preset ?? "clean");
  const base = CAPTION_PRESETS[presetName] ?? CAPTION_PRESETS.clean;
  const captions: CaptionStyle = { ...base, ...(input.captions ?? {}), preset: base.preset };

  const merged: VideoSettings = {
    ...DEFAULT_SETTINGS,
    ...input,
    language,
    duration_seconds: DURATIONS.includes(input.duration_seconds as never)
      ? (input.duration_seconds as number)
      : 30,
    aspect_ratio: ASPECT_RATIOS.some((a) => a.id === input.aspect_ratio)
      ? (input.aspect_ratio as AspectRatio)
      : "9:16",
    voice_rate: clamp(input.voice_rate, 0.5, 2, 1),
    voice_volume: clamp(input.voice_volume, 0, 1, 1),
    music_volume: clamp(input.music_volume, 0, 1, 0.12),
    clip_seconds: clamp(input.clip_seconds, 1.5, 10, 3.5),
    captions: {
      ...captions,
      size: clamp(captions.size, 24, 140, 64),
      outline_width: clamp(captions.outline_width, 0, 16, 4),
      words_per_line: clamp(captions.words_per_line, 1, 12, 4),
    },
  };
  return applyLanguage(merged, language);
}
