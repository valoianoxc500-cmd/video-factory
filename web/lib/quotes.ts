/**
 * Quote Studio: a person's photograph turned into a quote carousel.
 *
 * A separate product from the video engines. It queues no job, runs no
 * pipeline and never touches `factory.py` -- a carousel is finished in
 * seconds, and putting it behind the video worker's queue would make the
 * fastest thing here behave like the slowest.
 *
 * The one design decision everything else follows from: **the image model
 * never renders text.** It composes a portrait background and stops there;
 * the quote is laid over it in the browser. That is what makes the text
 * crisp at any size, editable after generation, correct in Arabic, and free
 * to re-typeset -- an image model asked for Arabic lettering returns
 * confident gibberish, and asked for English returns text that cannot be
 * edited without paying to regenerate the slide.
 */

export type QuoteLanguage = "ar" | "en";

export const QUOTE_LANGUAGES: {
  code: QuoteLanguage;
  label: string;
  native: string;
  dir: "rtl" | "ltr";
}[] = [
  { code: "en", label: "English", native: "English", dir: "ltr" },
  { code: "ar", label: "Arabic", native: "العربية", dir: "rtl" },
];

/** Slides per carousel. Instagram allows more; a quote deck stops landing after ~8. */
export const MIN_SLIDES = 5;
export const MAX_SLIDES = 8;

// ── typography ───────────────────────────────────────────────────────
//
// Arabic and English are not the same typographic problem, so they do not
// share a list. An Arabic quote set in a Latin display face falls back to a
// system font mid-sentence and the deck stops looking designed; every Arabic
// stack here is Arabic-first.

export type FontChoice = {
  id: string;
  label: string;
  hint: string;
  /** CSS font-family stack, applied to the quote itself. */
  stack: string;
  weight: number;
  /** Arabic needs more leading: stacked diacritics collide at Latin values. */
  lineHeight: number;
  letterSpacing: string;
};

export const FONTS: Record<QuoteLanguage, FontChoice[]> = {
  en: [
    {
      id: "editorial",
      label: "Editorial",
      hint: "High-contrast serif. Authority.",
      stack: "'Playfair Display', 'Georgia', serif",
      weight: 700,
      lineHeight: 1.16,
      letterSpacing: "-0.02em",
    },
    {
      id: "grotesk",
      label: "Grotesk",
      hint: "Tight sans. Modern and loud.",
      stack: "'Inter', 'Helvetica Neue', system-ui, sans-serif",
      weight: 800,
      lineHeight: 1.1,
      letterSpacing: "-0.035em",
    },
    {
      id: "mono",
      label: "Mono",
      hint: "Technical. Reads as a statement.",
      stack: "'JetBrains Mono', 'SF Mono', ui-monospace, monospace",
      weight: 600,
      lineHeight: 1.28,
      letterSpacing: "-0.01em",
    },
  ],
  ar: [
    {
      id: "naskh",
      label: "نسخ · Naskh",
      hint: "كلاسيكي وواضح. للاقتباسات الجادة.",
      stack: "'Noto Naskh Arabic', 'Traditional Arabic', serif",
      weight: 700,
      lineHeight: 1.75,
      letterSpacing: "0",
    },
    {
      id: "kufi",
      label: "كوفي · Kufi",
      hint: "هندسي وحديث. للعناوين القوية.",
      stack: "'Noto Kufi Arabic', 'Tahoma', sans-serif",
      weight: 700,
      lineHeight: 1.6,
      letterSpacing: "0",
    },
    {
      id: "cairo",
      label: "القاهرة · Cairo",
      hint: "عصري ومقروء على الهاتف.",
      stack: "'Cairo', 'Segoe UI', system-ui, sans-serif",
      weight: 800,
      lineHeight: 1.65,
      letterSpacing: "0",
    },
  ],
};

export function fontById(language: QuoteLanguage, id: string): FontChoice {
  const list = FONTS[language];
  return list.find((f) => f.id === id) ?? list[0];
}

// ── visual styles ────────────────────────────────────────────────────
//
// `prompt` is appended to the edit instruction. Each one describes light and
// treatment only -- never text, never a layout -- because the typography is
// composited afterwards and a model that renders its own words fights it.

export type VisualStyle = {
  id: string;
  label: string;
  hint: string;
  prompt: string;
  /** Overlay behind the text, so a quote stays readable over any photo. */
  scrim: string;
  ink: string;
  accent: string;
};

export const STYLES: VisualStyle[] = [
  {
    id: "noir",
    label: "Noir",
    hint: "Hard light, deep shadow, monochrome",
    prompt:
      "Convert to a high-contrast black and white editorial portrait. Hard " +
      "directional key light from one side, deep falloff into black, fine " +
      "film grain. Keep the background simple and dark.",
    scrim: "linear-gradient(180deg, rgba(0,0,0,.25) 0%, rgba(0,0,0,.88) 100%)",
    ink: "#FFFFFF",
    accent: "#C9A227",
  },
  {
    id: "warm",
    label: "Golden",
    hint: "Warm rim light, soft gradient",
    prompt:
      "Relight as a warm golden-hour editorial portrait. Soft amber rim " +
      "light behind the subject, gentle falloff, rich warm shadows, clean " +
      "uncluttered background.",
    scrim: "linear-gradient(180deg, rgba(26,12,0,.20) 0%, rgba(26,12,0,.86) 100%)",
    ink: "#FFF6E8",
    accent: "#FFB347",
  },
  {
    id: "studio",
    label: "Studio",
    hint: "Clean seamless backdrop",
    prompt:
      "Place the subject on a clean seamless studio backdrop in a deep " +
      "neutral tone. Soft large key light, subtle vignette, crisp modern " +
      "commercial portrait finish.",
    scrim: "linear-gradient(180deg, rgba(8,10,14,.18) 0%, rgba(8,10,14,.88) 100%)",
    ink: "#F2F5FA",
    accent: "#7FB2FF",
  },
  {
    id: "gradient",
    label: "Gradient",
    hint: "Saturated duotone wash",
    prompt:
      "Composite the subject over a bold saturated duotone gradient " +
      "background, magenta into deep violet. Edge light on the subject, " +
      "modern social-media poster treatment, clean and graphic.",
    scrim: "linear-gradient(180deg, rgba(20,0,30,.20) 0%, rgba(20,0,30,.85) 100%)",
    ink: "#FFFFFF",
    accent: "#FF7AD9",
  },
];

export function styleById(id: string): VisualStyle {
  return STYLES.find((s) => s.id === id) ?? STYLES[0];
}

// ── cost ─────────────────────────────────────────────────────────────

/**
 * Published fal price for `fal-ai/gemini-25-flash-image/edit`, the same model
 * and the same rate the thumbnail path is priced at.
 */
export const COST_PER_SLIDE_USD = 0.039;
/** One Gemini text call writes the whole quote set. */
export const COST_PER_QUOTE_BATCH_USD = 0.002;

export function estimateCost(slides: number): number {
  return Number(
    (slides * COST_PER_SLIDE_USD + COST_PER_QUOTE_BATCH_USD).toFixed(4),
  );
}

// ── the carousel ─────────────────────────────────────────────────────

export type Slide = {
  id: string;
  /** Quote text. Editable after generation; never baked into the image. */
  text: string;
  /** Background image as a data URI, or "" while pending. */
  image: string;
  status: "pending" | "generating" | "ready" | "failed";
  error?: string;
  /** The closing branded slide is composed differently and is never reordered away. */
  kind: "quote" | "outro";
};

export type QuoteBrief = {
  name: string;
  username: string;
  language: QuoteLanguage;
  topic: string;
  fontId: string;
  styleId: string;
};

/** `@handle`, with exactly one leading @ however the user typed it. */
export function normaliseHandle(raw: string): string {
  const trimmed = String(raw ?? "").trim().replace(/^@+/, "");
  return trimmed ? `@${trimmed}` : "";
}

export function isRtl(language: QuoteLanguage): boolean {
  return language === "ar";
}

/**
 * Whether a quote is short enough to set large.
 *
 * The failure this prevents is a paragraph rendered at 12px on a 9:16 slide,
 * which is the single most common way a quote carousel stops being readable.
 */
export function isQuoteSettable(text: string, language: QuoteLanguage): boolean {
  const t = String(text ?? "").trim();
  if (!t) return false;
  // Arabic runs shorter in characters for the same spoken length.
  return t.length <= (language === "ar" ? 150 : 180);
}
