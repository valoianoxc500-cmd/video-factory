/**
 * Clipping options, and the five states a customer is allowed to see.
 *
 * These ids are a contract with `viral/clipping.py`: the worker validates
 * whatever arrives and falls back to its own defaults, so a mismatch here
 * degrades the clip rather than failing it. They are kept in the same order
 * and with the same defaults so the screen and the worker agree about what
 * "balanced" means.
 */

export const ASPECTS = [
  { id: "9:16", label: "Vertical", hint: "Reels, Shorts, TikTok" },
  { id: "1:1", label: "Square", hint: "Feed" },
  { id: "4:5", label: "Portrait", hint: "Feed, taller" },
  { id: "16:9", label: "Landscape", hint: "YouTube, X" },
] as const;
export const DEFAULT_ASPECT = "9:16";

export const QUALITIES = [
  { id: "high", label: "High", hint: "Slowest, sharpest" },
  { id: "balanced", label: "Balanced", hint: "Recommended" },
  { id: "fast", label: "Fast", hint: "Quickest to produce" },
] as const;
export const DEFAULT_QUALITY = "balanced";

export const FOCUS_MODES = [
  { id: "auto", label: "Follow subject", hint: "Keeps the person in frame" },
  { id: "center", label: "Centre", hint: "Always the middle" },
  { id: "left", label: "Left", hint: "Fixed left" },
  { id: "right", label: "Right", hint: "Fixed right" },
] as const;
export const DEFAULT_FOCUS = "auto";

export const CAPTION_STYLES = [
  { id: "clean", label: "Clean" },
  { id: "bold", label: "Bold" },
  { id: "minimal", label: "Minimal" },
] as const;
export const DEFAULT_CAPTION_STYLE = "clean";

export type ClipOptions = {
  aspect: string;
  quality: string;
  focus: string;
  captions: boolean;
  caption_style: string;
  speaker: string;
};

export function defaultClipOptions(): ClipOptions {
  return {
    aspect: DEFAULT_ASPECT,
    quality: DEFAULT_QUALITY,
    focus: DEFAULT_FOCUS,
    captions: false,
    caption_style: DEFAULT_CAPTION_STYLE,
    speaker: "",
  };
}

/** The only states a customer is shown. */
export const CUSTOMER_STATES = [
  "Preparing",
  "Analyzing",
  "Creating clips",
  "Rendering",
  "Ready",
] as const;

export type CustomerState = (typeof CUSTOMER_STATES)[number];

/**
 * A task row turned into one of the five states.
 *
 * `running` is deliberately reported as "Creating clips" rather than guessed
 * at more precisely: the task row carries no sub-stage, and inventing
 * "Rendering" from nothing would be a progress bar that lies.
 */
export function customerState(
  status: string | null | undefined,
): CustomerState | null {
  switch (String(status ?? "").trim().toLowerCase()) {
    case "queued":
      return "Preparing";
    case "running":
      return "Creating clips";
    case "done":
      return "Ready";
    default:
      return null;
  }
}

/** Anything that would tell a customer about our tooling rather than their clip. */
const INTERNAL =
  /ffmpeg|ffprobe|libx264|traceback|stack|errno|exit code|codec|subprocess|stderr|https?:\/\/|[a-z]:\\|\/[\w.-]+\//i;

/**
 * A sentence a customer can read, from whatever the worker recorded.
 *
 * The worker already sanitises what it raises, so this is the second of two
 * gates rather than the only one -- an older task row, written before that
 * sanitising existed, can still carry a filter graph in its error column.
 */
export function safeClipError(raw: unknown): string {
  const text = String(raw ?? "").trim();
  if (!text) return "That clip could not be created. Nothing else was affected.";
  if (INTERNAL.test(text)) {
    return "That clip could not be created. Nothing else was affected.";
  }
  if (text.length > 200) {
    return "That clip could not be created. Nothing else was affected.";
  }
  return text;
}
