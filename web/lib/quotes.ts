/** Shared, deterministic Quote Studio contracts. */

import type { BackgroundId } from "./quote-backgrounds";

export { DEFAULT_BACKGROUND, normaliseBackground } from "./quote-backgrounds";
export type { BackgroundId } from "./quote-backgrounds";

export type QuoteLanguage = "ar" | "en";

export const QUOTE_LANGUAGES: { code: QuoteLanguage; label: string; native: string; dir: "rtl" | "ltr" }[] = [
  { code: "en", label: "English", native: "English", dir: "ltr" },
  { code: "ar", label: "Arabic", native: "العربية", dir: "rtl" },
];

export const MIN_QUOTES = 5;
export const MAX_QUOTES = 8;

export type FontChoice = {
  id: string;
  label: string;
  stack: string;
  weight: number;
  lineHeight: number;
  letterSpacing: string;
};

export const FONTS: Record<QuoteLanguage, FontChoice[]> = {
  en: [
    { id: "editorial", label: "Editorial serif", stack: "'Playfair Display', Georgia, serif", weight: 700, lineHeight: 1.14, letterSpacing: "-0.025em" },
    { id: "grotesk", label: "Modern grotesk", stack: "Inter, 'Helvetica Neue', system-ui, sans-serif", weight: 800, lineHeight: 1.08, letterSpacing: "-0.04em" },
  ],
  ar: [
    { id: "naskh", label: "نسخ · Naskh", stack: "'Noto Naskh Arabic', 'Traditional Arabic', serif", weight: 700, lineHeight: 1.72, letterSpacing: "0" },
    { id: "kufi", label: "كوفي · Kufi", stack: "'Noto Kufi Arabic', Tahoma, sans-serif", weight: 700, lineHeight: 1.58, letterSpacing: "0" },
  ],
};

export type QuoteProfile = { photo: string; displayName: string; username: string; preferredLanguage: QuoteLanguage };
export type QuoteCard = { id: string; text: string; selected: boolean; position: number };
export type QuoteProject = {
  id: string;
  topic: string;
  language: QuoteLanguage;
  fontId: string;
  ratio: "4:5" | "9:16";
  /**
   * The chosen background, from the local library in `quote-backgrounds`.
   * Saved with the project so reopening one shows the surface it was designed
   * on; a project saved before backgrounds existed reads back as white.
   */
  backgroundId: BackgroundId;
  profileSnapshot: QuoteProfile;
  quotes: QuoteCard[];
  createdAt: string;
  updatedAt: string;
};

export function fontById(language: QuoteLanguage, id: string): FontChoice {
  return FONTS[language].find((font) => font.id === id) ?? FONTS[language][0];
}

export function normaliseHandle(raw: string): string {
  const cleaned = String(raw ?? "").trim().replace(/^@+/, "");
  return cleaned ? `@${cleaned}` : "";
}

export function isRtl(language: QuoteLanguage): boolean { return language === "ar"; }

export function isQuoteSettable(text: string, language: QuoteLanguage): boolean {
  const quote = String(text ?? "").trim();
  return Boolean(quote) && quote.length <= (language === "ar" ? 150 : 180);
}

export function defaultProfile(): QuoteProfile {
  return { photo: "", displayName: "", username: "", preferredLanguage: "en" };
}

export function validLanguage(value: unknown): value is QuoteLanguage { return value === "ar" || value === "en"; }

export function normaliseCards(value: unknown, language: QuoteLanguage): QuoteCard[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  return value.slice(0, MAX_QUOTES).flatMap((item, index) => {
    if (!item || typeof item !== "object") return [];
    const row = item as Partial<QuoteCard>;
    const text = String(row.text ?? "").trim().slice(0, language === "ar" ? 150 : 180);
    const id = String(row.id ?? "").trim().slice(0, 80);
    if (!text || !id || seen.has(id)) return [];
    seen.add(id);
    return [{ id, text, selected: Boolean(row.selected), position: index }];
  });
}
