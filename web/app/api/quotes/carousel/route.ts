import { requireUser } from "@/lib/supabase/server";
import { rateLimit } from "@/lib/rate-limit";
import { QuoteTextError, writeQuoteText } from "@/lib/quote-text";
import { validLanguage, type QuoteLanguage } from "@/lib/quotes";
import {
  MAX_SLIDES,
  MIN_SLIDES,
  isTone,
  type SlideRole,
  type Tone,
} from "@/lib/carousel";

/**
 * Write one carousel, or rewrite one slide of it.
 *
 * Text only — the slides are drawn locally and for nothing, so this is the
 * single paid call in the whole studio.
 *
 * The important difference from the old quote route: this asks for **one
 * connected piece**, not N independent lines. A model asked for "5 quotes
 * about discipline" returns five openings; a model asked for a carousel with
 * a hook, a developing middle and a close returns something a person can
 * actually swipe through. The prompt below is built around that, and
 * `action: "slide"` rewrites a single slide *in the context of its
 * neighbours* so one edit cannot drift away from the rest.
 */

export const dynamic = "force-dynamic";

const LANGUAGE_RULES: Record<QuoteLanguage, string> = {
  en:
    "Write in English only. No Arabic characters anywhere in the output. " +
    "Plain, modern, confident English.",
  ar:
    "اكتب بالعربية الفصحى الحديثة فقط. لا تستخدم أي حرف لاتيني. " +
    "اكتب بأسلوب طبيعي يناسب منشورات التواصل الاجتماعي، وليس ترجمة حرفية.",
};

/** Shared rules, so a full generation and a single rewrite obey the same ones. */
const SHAPE_RULES = [
  "- No hashtags, no emoji, no quotation marks, no slide numbers, no labels.",
  "- Never invent statistics, studies, dates, names or citations.",
  "- Short lines. A slide is a graphic, not a paragraph.",
  "- Every slide must add something the others do not already say.",
];

const TONE_RULES: Record<Tone, string> = {
  educational: "Teach it plainly. Prefer concrete steps over abstractions.",
  motivational: "Push the reader to act. Urgent, but never hollow hype.",
  direct: "Blunt and unsentimental. No preamble, no softening.",
  storytelling: "Carry the reader through it, each slide following the last.",
};

function buildCarouselPrompt(
  idea: string,
  language: QuoteLanguage,
  tone: Tone,
  count: number | null,
): string {
  const length = count
    ? `Write exactly ${count} slides.`
    : `Choose a length between ${MIN_SLIDES} and ${MAX_SLIDES} slides — whichever the idea actually needs.`;

  return [
    `Write ONE social-media carousel about: ${idea}`,
    "",
    "This is a single connected piece, not a set of separate quotes. Someone",
    "reads slide 1, swipes because they want slide 2, and finishes having",
    "learned one thing.",
    "",
    length,
    LANGUAGE_RULES[language],
    `TONE: ${TONE_RULES[tone]}`,
    "",
    "STRUCTURE",
    "- Slide 1 is the HOOK. It creates curiosity or names a problem the",
    "  reader recognises. It does not summarise the whole carousel.",
    "- The middle slides carry the actual value: steps, reasons, lessons or",
    "  examples, whichever the topic calls for. Each one moves forward.",
    "- The last slide closes it: a takeaway, a question, or a call to act.",
    "  For example 'Save this for later.' or 'Which one will you start today?'",
    "",
    "RULES",
    ...SHAPE_RULES,
    "- Do not restate the hook in the final slide.",
    "",
    'Return ONLY a JSON array of strings, one per slide. No prose, no code fence.',
  ].join("\n");
}

function buildSlidePrompt(
  idea: string,
  language: QuoteLanguage,
  tone: Tone,
  role: SlideRole,
  before: string[],
  after: string[],
): string {
  const job =
    role === "hook"
      ? "This is the opening slide. Create curiosity; do not summarise."
      : role === "cta"
        ? "This is the closing slide. Land the point: a takeaway, a question, or a call to act."
        : "This is a middle slide. Carry the argument one step further.";

  return [
    `A social-media carousel about: ${idea}`,
    "",
    "Rewrite ONE slide of it. The rest of the carousel is staying exactly as",
    "it is, so the replacement has to fit between its neighbours.",
    "",
    before.length ? `SLIDES BEFORE IT:\n${before.map((t) => `- ${t}`).join("\n")}` : "It is the first slide.",
    after.length ? `SLIDES AFTER IT:\n${after.map((t) => `- ${t}`).join("\n")}` : "It is the last slide.",
    "",
    job,
    LANGUAGE_RULES[language],
    `TONE: ${TONE_RULES[tone]}`,
    "",
    "RULES",
    ...SHAPE_RULES,
    "- Do not repeat anything the surrounding slides already say.",
    "",
    "Return ONLY the replacement slide text. No JSON, no quotes, no label.",
  ].join("\n");
}

/** Model output is not trusted to be clean JSON; it usually is, sometimes isn't. */
function parseSlides(raw: string, max: number): string[] {
  let text = String(raw ?? "").trim();
  const fence = text.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fence) text = fence[1].trim();

  const start = text.indexOf("[");
  const end = text.lastIndexOf("]");
  if (start !== -1 && end > start) text = text.slice(start, end + 1);

  try {
    const parsed = JSON.parse(text);
    if (Array.isArray(parsed)) {
      return parsed.map((v) => String(v ?? "").trim()).filter(Boolean).slice(0, max);
    }
  } catch {
    // Fall through to the line reader.
  }

  // A model that ignored the format still wrote usable slides; losing them to
  // a parse error helps nobody.
  return String(raw ?? "")
    .split("\n")
    .map((line) =>
      line
        .replace(/^\s*[-*•]\s*/, "")
        .replace(/^\s*\d+[.)]\s*/, "")
        .replace(/^["“”']|["“”']$/g, "")
        .trim(),
    )
    .filter((line) => line.length > 8)
    .slice(0, max);
}

/** One slide's text, from a model that may still have wrapped it. */
function parseOneSlide(raw: string): string {
  const lines = parseSlides(raw, 1);
  if (lines.length) return lines[0];
  return String(raw ?? "").trim().replace(/^["“”']|["“”']$/g, "").slice(0, 400);
}

/**
 * Which language to write in when the user chose "Auto".
 *
 * Detected from the idea itself: somebody who typed the question in Arabic
 * wants an Arabic carousel, and asking them to say so twice is a worse
 * default than reading what they already wrote.
 */
function detectLanguage(idea: string): QuoteLanguage {
  return /[؀-ۿ]/.test(String(idea ?? "")) ? "ar" : "en";
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();

    const gate = rateLimit(`carousel:${user.id}`, { limit: 20, windowSeconds: 60 });
    if (!gate.allowed) {
      return Response.json(
        { error: "Slow down a moment." },
        { status: 429, headers: { "Retry-After": String(gate.retryAfterSeconds) } },
      );
    }

    let body: Record<string, unknown>;
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      return Response.json({ error: "Invalid request body." }, { status: 400 });
    }

    const idea = String(body.idea ?? "").trim();
    if (!idea) {
      return Response.json(
        { error: "Tell us what the carousel should be about." },
        { status: 400 },
      );
    }
    if (idea.length > 300) {
      return Response.json({ error: "That idea is too long." }, { status: 400 });
    }

    const requested = body.language;
    const language: QuoteLanguage =
      requested === "auto" || requested === undefined || requested === null
        ? detectLanguage(idea)
        : validLanguage(requested)
          ? requested
          : "en";

    const tone: Tone = isTone(body.tone) ? body.tone : "educational";
    const action = String(body.action ?? "carousel").trim().toLowerCase();

    // ── rewrite one slide, in context ─────────────────────────────
    if (action === "slide") {
      const role = String(body.role ?? "body") as SlideRole;
      const before = Array.isArray(body.before)
        ? body.before.map((v) => String(v ?? "").trim()).filter(Boolean).slice(-3)
        : [];
      const after = Array.isArray(body.after)
        ? body.after.map((v) => String(v ?? "").trim()).filter(Boolean).slice(0, 3)
        : [];

      const raw = await writeQuoteText(
        buildSlidePrompt(idea, language, tone, role, before, after),
      );
      const text = parseOneSlide(raw);
      if (!text) {
        return Response.json(
          { error: "Could not rewrite that slide. Try again." },
          { status: 502 },
        );
      }
      return Response.json({ text, language });
    }

    // ── the whole carousel ────────────────────────────────────────
    const rawCount = body.slides;
    const count =
      rawCount === "auto" || rawCount === undefined || rawCount === null
        ? null
        : Math.min(MAX_SLIDES, Math.max(MIN_SLIDES, Number(rawCount) || MIN_SLIDES));

    const raw = await writeQuoteText(
      buildCarouselPrompt(idea, language, tone, count),
    );
    const slides = parseSlides(raw, count ?? MAX_SLIDES);

    // Below the minimum there is no carousel to review — better to say so
    // than to hand back two slides and call it one.
    if (slides.length < MIN_SLIDES) {
      return Response.json(
        { error: "Could not write a full carousel. Try rephrasing the idea." },
        { status: 502 },
      );
    }

    return Response.json({ slides, language, tone });
  } catch (err) {
    if (err instanceof QuoteTextError) {
      return Response.json({ error: err.message }, { status: err.status });
    }
    const message = (err as Error)?.message ?? "";
    const isAuth = /auth|session|user/i.test(message);
    return Response.json(
      {
        error: isAuth
          ? "You must be signed in to do that."
          : "Could not write the carousel right now.",
      },
      { status: isAuth ? 401 : 500 },
    );
  }
}
