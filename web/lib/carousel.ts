/**
 * The carousel model: one idea, several slides, written as one piece.
 *
 * Quote Studio used to produce a set of independent aphorisms about a topic.
 * A carousel is the opposite shape: slide one earns the swipe, the middle
 * slides carry one argument forward without repeating each other, and the
 * last one lands it. Slides that could be shuffled at random are quotes, not
 * a carousel — which is why `role` below is part of the model rather than a
 * label added afterwards.
 *
 * Everything here is additive to the existing project format. An old project
 * has no roles and no per-slide backgrounds; `slidesFromCards` gives it both
 * without rewriting anything in storage, so opening a year-old quote deck
 * still works and still renders.
 */

import {
  DEFAULT_BACKGROUND,
  normaliseBackground,
  type BackgroundId,
} from "./quote-backgrounds";
import type { QuoteCard, QuoteLanguage } from "./quotes";

/** Where a slide sits in the argument. */
export type SlideRole = "hook" | "body" | "cta";

export const MIN_SLIDES = 5;
export const MAX_SLIDES = 8;

/** "Auto" lets the model choose a length that fits the idea. */
export type SlideCount = number | "auto";

export const SLIDE_COUNTS: { id: string; label: string }[] = [
  { id: "auto", label: "Auto" },
  { id: "5", label: "5" },
  { id: "6", label: "6" },
  { id: "7", label: "7" },
  { id: "8", label: "8" },
];

export type Tone = "educational" | "motivational" | "direct" | "storytelling";

export const TONES: { id: Tone; label: string; hint: string }[] = [
  { id: "educational", label: "Educational", hint: "Explain it clearly" },
  { id: "motivational", label: "Motivational", hint: "Push them to act" },
  { id: "direct", label: "Direct", hint: "Blunt, no preamble" },
  { id: "storytelling", label: "Storytelling", hint: "Carry them through it" },
];

export const DEFAULT_TONE: Tone = "educational";

/** "Auto" detects the language from the idea the user typed. */
export type CarouselLanguage = QuoteLanguage | "auto";

export const CAROUSEL_LANGUAGES: { id: CarouselLanguage; label: string; native: string }[] = [
  { id: "auto", label: "Auto", native: "Auto" },
  { id: "en", label: "English", native: "English" },
  { id: "ar", label: "Arabic", native: "العربية" },
];

/** Example ideas, shown as placeholders so the input is never a blank stare. */
export const IDEA_EXAMPLES = [
  "How to become a better person",
  "5 habits that improve your life",
  "How to grow on Instagram",
  "Why you always feel tired",
  "How to become more disciplined",
];

// ── slides ───────────────────────────────────────────────────────────

export type Slide = {
  id: string;
  text: string;
  role: SlideRole;
  /**
   * Per-slide background. A carousel usually wants one surface throughout,
   * but a hook or a closing slide often earns its own — so the background
   * lives on the slide and the project's `backgroundId` is the default a new
   * slide inherits.
   */
  background: BackgroundId;
};

export function isTone(value: unknown): value is Tone {
  return TONES.some((t) => t.id === value);
}

export function isSlideRole(value: unknown): value is SlideRole {
  return value === "hook" || value === "body" || value === "cta";
}

/** The role a slide at this position should have, given the total. */
export function roleForIndex(index: number, total: number): SlideRole {
  if (index === 0) return "hook";
  if (index === total - 1) return "cta";
  return "body";
}

/**
 * Re-derive roles after a reorder or a delete.
 *
 * Roles are positional, not sticky: dragging the closing slide to the top
 * makes it the hook, because that is what it now is. Keeping the old label
 * would leave a carousel with two CTAs and no opening.
 */
export function withRoles(slides: Slide[]): Slide[] {
  return slides.map((slide, index) => ({
    ...slide,
    role: roleForIndex(index, slides.length),
  }));
}

let counter = 0;
/** A slide id unique within a session. Stable once assigned. */
export function slideId(): string {
  counter += 1;
  return `s${Date.now().toString(36)}${counter.toString(36)}`;
}

export function makeSlide(
  text: string,
  role: SlideRole,
  background: BackgroundId = DEFAULT_BACKGROUND,
): Slide {
  return { id: slideId(), text: String(text ?? "").trim(), role, background };
}

// ── storage, both directions ─────────────────────────────────────────

/**
 * Slides from a stored project's cards.
 *
 * The stored shape is the existing `QuoteCard`, so nothing in the database
 * changes. Older rows carry no role and no background: roles are re-derived
 * from position and the background falls back to the project's, which is
 * exactly what those projects were rendered with before slides could differ.
 */
export function slidesFromCards(
  cards: QuoteCard[],
  projectBackground: BackgroundId = DEFAULT_BACKGROUND,
): Slide[] {
  const rows = [...(cards ?? [])].sort(
    (a, b) => Number(a.position ?? 0) - Number(b.position ?? 0),
  );
  return rows.map((card, index) => {
    const extra = card as QuoteCard & { role?: unknown; background?: unknown };
    return {
      id: String(card.id ?? slideId()),
      text: String(card.text ?? ""),
      role: isSlideRole(extra.role) ? extra.role : roleForIndex(index, rows.length),
      background: normaliseBackground(extra.background ?? projectBackground),
    };
  });
}

/**
 * Cards to store, from slides.
 *
 * `role` and `background` are written alongside the fields the old format
 * already had. A reader that does not know about them ignores them; a reader
 * that does gets the carousel back exactly as designed.
 */
export function cardsFromSlides(slides: Slide[]): (QuoteCard & {
  role: SlideRole;
  background: BackgroundId;
})[] {
  return slides.map((slide, position) => ({
    id: slide.id,
    text: slide.text,
    selected: true,
    position,
    role: slide.role,
    background: slide.background,
  }));
}

// ── shape checks ─────────────────────────────────────────────────────

/**
 * Whether a slide's text will still look like a graphic rather than a page.
 *
 * A hook has to be readable in the half-second someone spends deciding to
 * swipe, so it is held tighter than the body slides.
 */
export function slideFits(text: string, role: SlideRole, language: QuoteLanguage): boolean {
  const value = String(text ?? "").trim();
  if (!value) return false;
  const arabic = language === "ar";
  const limit = role === "hook" ? (arabic ? 90 : 110) : arabic ? 190 : 230;
  return value.length <= limit;
}

/** Normalise whatever the model returned into a usable carousel. */
export function normaliseSlides(
  texts: unknown,
  background: BackgroundId = DEFAULT_BACKGROUND,
): Slide[] {
  if (!Array.isArray(texts)) return [];
  const cleaned = texts
    .map((value) => String(value ?? "").trim())
    .filter(Boolean)
    .slice(0, MAX_SLIDES);
  return cleaned.map((text, index) =>
    makeSlide(text, roleForIndex(index, cleaned.length), background),
  );
}

/**
 * Ideas repeated across slides, as normalised text.
 *
 * A carousel that says the same thing twice reads as padding, and it is the
 * most common way a per-slide generator fails. Compared on content words so
 * "keep your promises" and "Keep the promises!" count as one idea.
 */
export function duplicateIdeas(slides: Slide[]): string[] {
  const seen = new Map<string, number>();
  for (const slide of slides) {
    const key = ideaKey(slide.text);
    if (!key) continue;
    seen.set(key, (seen.get(key) ?? 0) + 1);
  }
  return [...seen.entries()].filter(([, n]) => n > 1).map(([key]) => key);
}

const STOP_WORDS = new Set([
  "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "is",
  "are", "be", "you", "your", "it", "that", "this", "with", "as", "at", "by",
]);

function ideaKey(text: string): string {
  return String(text ?? "")
    .toLowerCase()
    .replace(/[^\p{L}\p{N}\s]/gu, " ")
    .split(/\s+/)
    .filter((word) => word && !STOP_WORDS.has(word))
    .sort()
    .join(" ");
}

// ── captions ─────────────────────────────────────────────────────────

/** Platforms a caption can be shaped for. Publishing support is separate. */
export const CAPTION_TARGETS = ["instagram", "facebook", "threads", "x"] as const;
export type CaptionTarget = (typeof CAPTION_TARGETS)[number];

/**
 * How long a caption may be on each platform.
 *
 * Used to trim a suggestion before it is shown, so the user never edits text
 * that would be cut off at publish time.
 */
export const CAPTION_LIMITS: Record<CaptionTarget, number> = {
  instagram: 2200,
  facebook: 2000,
  threads: 500,
  x: 280,
};
