import { requireUser } from "@/lib/supabase/server";
import { rateLimit } from "@/lib/rate-limit";
import { QuoteTextError, writeQuoteText } from "@/lib/quote-text";
import {
  MAX_QUOTES,
  MIN_QUOTES,
  QUOTE_LANGUAGES,
  type QuoteLanguage,
} from "@/lib/quotes";

/**
 * Quote Studio: write the quotes.
 *
 * Text only. This route never touches an image, and the image route never
 * writes text -- the two halves stay separate so a user can rewrite a quote
 * without paying to regenerate its slide, which is the whole reason the
 * typography is composited in the browser rather than drawn by the model.
 */

export const dynamic = "force-dynamic";

const LANGUAGE_RULES: Record<QuoteLanguage, string> = {
  en:
    "Write in English only. No Arabic characters anywhere in the output. " +
    "Plain, forceful, modern English.",
  ar:
    "اكتب بالعربية الفصحى فقط. لا تستخدم أي حرف لاتيني في المخرجات. " +
    "لغة قوية وواضحة وحديثة.",
};

function buildPrompt(
  topic: string,
  name: string,
  language: QuoteLanguage,
  count: number,
): string {
  return [
    `Write ${count} short, powerful, original quotes about: ${topic}`,
    "",
    `They are presented as the voice of ${name} on a social-media quote card.`,
    LANGUAGE_RULES[language],
    "",
    "RULES",
    "- Each quote stands alone and makes one point.",
    `- Hard maximum ${language === "ar" ? 90 : 110} characters each. Shorter is better.`,
    "- No hashtags, no emoji, no quotation marks, no attribution, no numbering.",
    "- Never invent a biographical fact, statistic, date or event about the person.",
    "- Write an aphorism, not a claim about what they did.",
    "- Every quote must be different in idea, not just in wording.",
    "",
    "Return ONLY a JSON array of strings. No prose, no code fence.",
  ].join("\n");
}

/** Model output is not trusted to be clean JSON; it usually is, sometimes isn't. */
function parseQuotes(raw: string, count: number): string[] {
  let text = raw.trim();
  const fence = text.match(/```(?:json)?\s*([\s\S]*?)```/);
  if (fence) text = fence[1].trim();

  const start = text.indexOf("[");
  const end = text.lastIndexOf("]");
  if (start !== -1 && end > start) text = text.slice(start, end + 1);

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    // Fall back to lines: a model that ignored the format still wrote usable
    // quotes, and losing them to a parse error helps nobody.
    return raw
      .split("\n")
      .map((l) => l.replace(/^\s*[-*\d.)\]]+\s*/, "").replace(/^["“”]|["“”]$/g, "").trim())
      .filter((l) => l.length > 8)
      .slice(0, count);
  }

  if (!Array.isArray(parsed)) return [];
  return parsed
    .map((q) => String(q ?? "").trim())
    .filter(Boolean)
    .slice(0, count);
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();

    const gate = rateLimit(`quotes:${user.id}`, { limit: 12, windowSeconds: 60 });
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

    const topic = String(body.topic ?? "").trim();
    const name = String(body.name ?? "").trim();
    const language = String(body.language ?? "en") as QuoteLanguage;
    const count = Math.min(
      MAX_QUOTES,
      Math.max(MIN_QUOTES, Number(body.count ?? 6) || 6),
    );

    if (!topic) {
      return Response.json({ error: "Enter a topic or idea." }, { status: 400 });
    }
    if (topic.length > 300) {
      return Response.json({ error: "That topic is too long." }, { status: 400 });
    }
    if (!QUOTE_LANGUAGES.some((l) => l.code === language)) {
      return Response.json({ error: "Unknown language." }, { status: 400 });
    }

    // One extra, so the user has something to swap in without regenerating.
    const raw = await writeQuoteText(
      buildPrompt(topic, name || "the speaker", language, count + 2),
    );
    const quotes = parseQuotes(raw, count + 2);

    if (!quotes.length) {
      return Response.json(
        { error: "Could not write usable quotes. Try rephrasing the topic." },
        { status: 502 },
      );
    }

    return Response.json({ quotes });
  } catch (err) {
    if (err instanceof QuoteTextError) {
      return Response.json({ error: err.message }, { status: err.status });
    }
    const message = (err as Error)?.message ?? "";
    return Response.json(
      { error: /auth|session|user/i.test(message) ? "You must be signed in to do that." : "Could not write quotes right now." },
      { status: /auth|session|user/i.test(message) ? 401 : 500 },
    );
  }
}
