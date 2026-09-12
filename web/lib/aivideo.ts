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
  /** Provider identifier. Internal only — never shown to a customer. */
  id: string;
  /** The person's name, e.g. "Salma". */
  label: string;
  language: Language;
  /** BCP-47 locale, used to group the picker. */
  locale: string;
  /** Customer-facing accent, e.g. "Egyptian", "British". */
  accent: string;
  gender: "male" | "female";
  note: string;
};

export const VOICES: Voice[] = [
  { id: "ar-EG-SalmaNeural", label: "Salma", language: "ar", locale: "ar-EG", accent: "Egyptian", gender: "female", note: "Warm, clear" },
  { id: "ar-EG-ShakirNeural", label: "Shakir", language: "ar", locale: "ar-EG", accent: "Egyptian", gender: "male", note: "News delivery" },
  { id: "ar-SA-ZariyahNeural", label: "Zariyah", language: "ar", locale: "ar-SA", accent: "Gulf / Modern Standard", gender: "female", note: "Formal, precise" },
  { id: "ar-SA-HamedNeural", label: "Hamed", language: "ar", locale: "ar-SA", accent: "Gulf / Modern Standard", gender: "male", note: "Deep, authoritative" },
  { id: "ar-AE-FatimaNeural", label: "Fatima", language: "ar", locale: "ar-AE", accent: "Emirati", gender: "female", note: "Bright, friendly" },
  { id: "ar-AE-HamdanNeural", label: "Hamdan", language: "ar", locale: "ar-AE", accent: "Emirati", gender: "male", note: "Calm narrator" },
  { id: "en-US-AriaNeural", label: "Aria", language: "en", locale: "en-US", accent: "American", gender: "female", note: "Warm, natural" },
  { id: "en-US-GuyNeural", label: "Guy", language: "en", locale: "en-US", accent: "American", gender: "male", note: "Confident narrator" },
  { id: "en-US-JennyNeural", label: "Jenny", language: "en", locale: "en-US", accent: "American", gender: "female", note: "Friendly, upbeat" },
  { id: "en-US-ChristopherNeural", label: "Christopher", language: "en", locale: "en-US", accent: "American", gender: "male", note: "Deep documentary" },
  { id: "en-GB-SoniaNeural", label: "Sonia", language: "en", locale: "en-GB", accent: "British", gender: "female", note: "Crisp, composed" },
  { id: "en-GB-RyanNeural", label: "Ryan", language: "en", locale: "en-GB", accent: "British", gender: "male", note: "Measured, warm" },
  { id: "ar-BH-AliNeural", label: "Ali", language: "ar", locale: "ar-BH", accent: "Bahraini", gender: "male", note: "Bahraini" },
  { id: "ar-BH-LailaNeural", label: "Laila", language: "ar", locale: "ar-BH", accent: "Bahraini", gender: "female", note: "Bahraini" },
  { id: "ar-DZ-AminaNeural", label: "Amina", language: "ar", locale: "ar-DZ", accent: "Algerian", gender: "female", note: "Algerian" },
  { id: "ar-DZ-IsmaelNeural", label: "Ismael", language: "ar", locale: "ar-DZ", accent: "Algerian", gender: "male", note: "Algerian" },
  { id: "ar-IQ-BasselNeural", label: "Bassel", language: "ar", locale: "ar-IQ", accent: "Iraqi", gender: "male", note: "Iraqi" },
  { id: "ar-IQ-RanaNeural", label: "Rana", language: "ar", locale: "ar-IQ", accent: "Iraqi", gender: "female", note: "Iraqi" },
  { id: "ar-JO-SanaNeural", label: "Sana", language: "ar", locale: "ar-JO", accent: "Jordanian", gender: "female", note: "Jordanian" },
  { id: "ar-JO-TaimNeural", label: "Taim", language: "ar", locale: "ar-JO", accent: "Jordanian", gender: "male", note: "Jordanian" },
  { id: "ar-KW-FahedNeural", label: "Fahed", language: "ar", locale: "ar-KW", accent: "Kuwaiti", gender: "male", note: "Kuwaiti" },
  { id: "ar-KW-NouraNeural", label: "Noura", language: "ar", locale: "ar-KW", accent: "Kuwaiti", gender: "female", note: "Kuwaiti" },
  { id: "ar-LB-LaylaNeural", label: "Layla", language: "ar", locale: "ar-LB", accent: "Lebanese", gender: "female", note: "Lebanese" },
  { id: "ar-LB-RamiNeural", label: "Rami", language: "ar", locale: "ar-LB", accent: "Lebanese", gender: "male", note: "Lebanese" },
  { id: "ar-LY-ImanNeural", label: "Iman", language: "ar", locale: "ar-LY", accent: "Libyan", gender: "female", note: "Libyan" },
  { id: "ar-LY-OmarNeural", label: "Omar", language: "ar", locale: "ar-LY", accent: "Libyan", gender: "male", note: "Libyan" },
  { id: "ar-MA-JamalNeural", label: "Jamal", language: "ar", locale: "ar-MA", accent: "Moroccan", gender: "male", note: "Moroccan" },
  { id: "ar-MA-MounaNeural", label: "Mouna", language: "ar", locale: "ar-MA", accent: "Moroccan", gender: "female", note: "Moroccan" },
  { id: "ar-OM-AbdullahNeural", label: "Abdullah", language: "ar", locale: "ar-OM", accent: "Omani", gender: "male", note: "Omani" },
  { id: "ar-OM-AyshaNeural", label: "Aysha", language: "ar", locale: "ar-OM", accent: "Omani", gender: "female", note: "Omani" },
  { id: "ar-QA-AmalNeural", label: "Amal", language: "ar", locale: "ar-QA", accent: "Qatari", gender: "female", note: "Qatari" },
  { id: "ar-QA-MoazNeural", label: "Moaz", language: "ar", locale: "ar-QA", accent: "Qatari", gender: "male", note: "Qatari" },
  { id: "ar-SY-AmanyNeural", label: "Amany", language: "ar", locale: "ar-SY", accent: "Syrian", gender: "female", note: "Syrian" },
  { id: "ar-SY-LaithNeural", label: "Laith", language: "ar", locale: "ar-SY", accent: "Syrian", gender: "male", note: "Syrian" },
  { id: "ar-TN-HediNeural", label: "Hedi", language: "ar", locale: "ar-TN", accent: "Tunisian", gender: "male", note: "Tunisian" },
  { id: "ar-TN-ReemNeural", label: "Reem", language: "ar", locale: "ar-TN", accent: "Tunisian", gender: "female", note: "Tunisian" },
  { id: "ar-YE-MaryamNeural", label: "Maryam", language: "ar", locale: "ar-YE", accent: "Yemeni", gender: "female", note: "Yemeni" },
  { id: "ar-YE-SalehNeural", label: "Saleh", language: "ar", locale: "ar-YE", accent: "Yemeni", gender: "male", note: "Yemeni" },
  { id: "en-AU-NatashaNeural", label: "Natasha", language: "en", locale: "en-AU", accent: "Australian", gender: "female", note: "Australian" },
  { id: "en-AU-WilliamMultilingualNeural", label: "William", language: "en", locale: "en-AU", accent: "Australian", gender: "male", note: "Australian" },
  { id: "en-CA-ClaraNeural", label: "Clara", language: "en", locale: "en-CA", accent: "Canadian", gender: "female", note: "Canadian" },
  { id: "en-CA-LiamNeural", label: "Liam", language: "en", locale: "en-CA", accent: "Canadian", gender: "male", note: "Canadian" },
  { id: "en-GB-LibbyNeural", label: "Libby", language: "en", locale: "en-GB", accent: "British", gender: "female", note: "British" },
  { id: "en-GB-MaisieNeural", label: "Maisie", language: "en", locale: "en-GB", accent: "British", gender: "female", note: "British" },
  { id: "en-GB-ThomasNeural", label: "Thomas", language: "en", locale: "en-GB", accent: "British", gender: "male", note: "British" },
  { id: "en-IE-ConnorNeural", label: "Connor", language: "en", locale: "en-IE", accent: "Irish", gender: "male", note: "Irish" },
  { id: "en-IE-EmilyNeural", label: "Emily", language: "en", locale: "en-IE", accent: "Irish", gender: "female", note: "Irish" },
  { id: "en-US-AnaNeural", label: "Ana", language: "en", locale: "en-US", accent: "American", gender: "female", note: "American" },
  { id: "en-US-AndrewNeural", label: "Andrew", language: "en", locale: "en-US", accent: "American", gender: "male", note: "American" },
  { id: "en-US-AndrewMultilingualNeural", label: "Andrew", language: "en", locale: "en-US", accent: "American", gender: "male", note: "American" },
  { id: "en-US-AvaNeural", label: "Ava", language: "en", locale: "en-US", accent: "American", gender: "female", note: "American" },
  { id: "en-US-AvaMultilingualNeural", label: "Ava", language: "en", locale: "en-US", accent: "American", gender: "female", note: "American" },
  { id: "en-US-BrianNeural", label: "Brian", language: "en", locale: "en-US", accent: "American", gender: "male", note: "American" },
  { id: "en-US-BrianMultilingualNeural", label: "Brian", language: "en", locale: "en-US", accent: "American", gender: "male", note: "American" },
  { id: "en-US-EmmaNeural", label: "Emma", language: "en", locale: "en-US", accent: "American", gender: "female", note: "American" },
  { id: "en-US-EmmaMultilingualNeural", label: "Emma", language: "en", locale: "en-US", accent: "American", gender: "female", note: "American" },
  { id: "en-US-EricNeural", label: "Eric", language: "en", locale: "en-US", accent: "American", gender: "male", note: "American" },
  { id: "en-US-MichelleNeural", label: "Michelle", language: "en", locale: "en-US", accent: "American", gender: "female", note: "American" },
  { id: "en-US-RogerNeural", label: "Roger", language: "en", locale: "en-US", accent: "American", gender: "male", note: "American" },
  { id: "en-US-SteffanNeural", label: "Steffan", language: "en", locale: "en-US", accent: "American", gender: "male", note: "American" },
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
  clean: { label: "Clean", preset: "clean", font: "inter", size: 64, weight: "bold", text_color: "#FFFFFF", highlight_color: "#FFFFFF", outline_color: "#000000", outline_width: 4, background: "none", position: "bottom", words_per_line: 4, uppercase: false, enabled: true },
  bold: { label: "Bold", preset: "bold", font: "montserrat", size: 76, weight: "black", text_color: "#FFFFFF", highlight_color: "#FFE500", outline_color: "#000000", outline_width: 7, background: "none", position: "center", words_per_line: 3, uppercase: true, enabled: true },
  viral: { label: "Viral", preset: "viral", font: "montserrat", size: 82, weight: "black", text_color: "#FFFFFF", highlight_color: "#00E676", outline_color: "#000000", outline_width: 8, background: "none", position: "center", words_per_line: 3, uppercase: true, enabled: true },
  minimal: { label: "Minimal", preset: "minimal", font: "inter", size: 52, weight: "medium", text_color: "#FFFFFF", highlight_color: "#FFFFFF", outline_color: "#000000", outline_width: 2, background: "none", position: "bottom", words_per_line: 6, uppercase: false, enabled: true },
  boxed: { label: "Boxed", preset: "boxed", font: "inter", size: 60, weight: "bold", text_color: "#FFFFFF", highlight_color: "#FFD600", outline_color: "#000000", outline_width: 0, background: "#000000CC", position: "bottom", words_per_line: 5, uppercase: false, enabled: true },
  karaoke: { label: "Karaoke", preset: "karaoke", font: "montserrat", size: 70, weight: "bold", text_color: "#FFFFFF", highlight_color: "#FF3D71", outline_color: "#000000", outline_width: 6, background: "none", position: "center", words_per_line: 4, uppercase: false, enabled: true },
  cinematic: { label: "Cinematic", preset: "cinematic", font: "playfair", size: 56, weight: "medium", text_color: "#F5F0E6", highlight_color: "#E0C070", outline_color: "#000000", outline_width: 3, background: "none", position: "bottom", words_per_line: 5, uppercase: false, enabled: true },
  arabic_clean: { label: "Arabic Clean", preset: "arabic_clean", font: "notosansarabic", size: 68, weight: "bold", text_color: "#FFFFFF", highlight_color: "#FFFFFF", outline_color: "#000000", outline_width: 4, background: "none", position: "bottom", words_per_line: 5, uppercase: false, enabled: true },
  arabic_bold: { label: "Arabic Bold", preset: "arabic_bold", font: "notosansarabic", size: 78, weight: "black", text_color: "#FFFFFF", highlight_color: "#FFE500", outline_color: "#000000", outline_width: 6, background: "none", position: "center", words_per_line: 4, uppercase: false, enabled: true },
  arabic_viral: { label: "Arabic Viral", preset: "arabic_viral", font: "notosansarabic", size: 84, weight: "black", text_color: "#FFFFFF", highlight_color: "#00E676", outline_color: "#000000", outline_width: 7, background: "none", position: "center", words_per_line: 4, uppercase: false, enabled: true },
  arabic_boxed: { label: "Arabic Boxed", preset: "arabic_boxed", font: "notokufiarabic", size: 64, weight: "bold", text_color: "#FFFFFF", highlight_color: "#FFD600", outline_color: "#000000", outline_width: 0, background: "#000000CC", position: "bottom", words_per_line: 5, uppercase: false, enabled: true },
};

/** Families that can shape Arabic; mirrors ARABIC_CAPABLE_FONTS in spec.py. */
export const ARABIC_FONTS = ["notosansarabic", "notokufiarabic", "arial", "tahoma"];

/**
 * Caption families the customer can pick.
 *
 * `stack` names the same faces the renderer uses, and the Arabic ones are
 * served from /public/fonts as @font-face in aivideo.css — so the preview and
 * the burned-in caption are the same typeface, not a lookalike.
 */
export const FONTS: {
  id: string;
  label: string;
  arabic: boolean;
  stack: string;
  weight?: number;
}[] = [
  { id: "inter", label: "Inter", arabic: false, stack: "'Inter', system-ui, sans-serif" },
  { id: "montserrat", label: "Montserrat", arabic: false, stack: "'Montserrat', system-ui, sans-serif" },
  { id: "playfair", label: "Playfair", arabic: false, stack: "'Playfair Display', Georgia, serif" },
  { id: "notosansarabic", label: "Noto Sans Arabic", arabic: true, stack: "'Noto Sans Arabic', Tahoma, sans-serif", weight: 700 },
  { id: "notokufiarabic", label: "Noto Kufi Arabic", arabic: true, stack: "'Noto Kufi Arabic', Tahoma, sans-serif", weight: 700 },
  { id: "arial", label: "Arial", arabic: true, stack: "Arial, Helvetica, sans-serif" },
  { id: "tahoma", label: "Tahoma", arabic: true, stack: "Tahoma, Verdana, sans-serif" },
];

export function fontWeightFor(id: string): number | undefined {
  return FONTS.find((f) => f.id === id)?.weight;
}

/**
 * Nominal size multiplier per family; mirrors medialab.arabic._SIZE_SCALE.
 *
 * The Noto Arabic faces draw about a third smaller than the Latin ones at the
 * same nominal size, because their vertical metrics leave room for stacked
 * marks. The renderer compensates, so the preview has to as well or the two
 * stop matching.
 */
export const FONT_SIZE_SCALE: Record<string, number> = {
  notosansarabic: 1.45,
  notokufiarabic: 1.45,
};

export function fontScaleFor(id: string): number {
  return FONT_SIZE_SCALE[id] ?? 1;
}

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
  /**
   * Set by the read path when a job has sat unclaimed, or claimed and silent,
   * past the bounded threshold — the shape a down worker takes, which nothing
   * in the pipeline can report because the pipeline never ran. The job is
   * untouched and still recoverable; this only changes what the customer is
   * told.
   */
  stalled?: boolean;
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

export function progressLabel(
  job: Pick<VideoJob, "status" | "stage" | "message"> & { stalled?: boolean },
): string {
  if (job.status === "done") return "Ready";
  if (job.status === "error") return "Needs another try";
  // A stalled job is still queued, so the honest label is "delayed", not
  // "failed" — it will finish on its own once a worker is back.
  if (job.stalled) return "Delayed — still queued";
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
