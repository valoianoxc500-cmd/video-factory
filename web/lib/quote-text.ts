/** Text-only adapter for Quote Studio's existing model gateway. */

const TEXT_URL = "https://fal.run/fal-ai/any-llm";
const TIMEOUT_MS = 45_000;

export class QuoteTextError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "QuoteTextError";
  }
}

/** Quote card rendering is local; this call returns text only. */
export async function writeQuoteText(prompt: string): Promise<string> {
  const key = process.env.FAL_KEY ?? "";
  if (!key) throw new QuoteTextError("Quote writing is temporarily unavailable.", 503);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetch(TEXT_URL, {
      method: "POST",
      headers: { Authorization: `Key ${key}`, "Content-Type": "application/json" },
      body: JSON.stringify({ model: "google/gemini-flash-1.5", prompt }),
      signal: controller.signal,
    });
    if (!response.ok) {
      throw new QuoteTextError(
        response.status === 429 ? "Quote writing is busy. Please try again shortly." : "Quote writing is temporarily unavailable.",
        response.status === 429 ? 429 : 502,
      );
    }
    const body = (await response.json()) as Record<string, unknown>;
    const text = String(body.output ?? "").trim();
    if (!text) throw new QuoteTextError("Quote writing returned no usable text.", 502);
    return text;
  } catch (error) {
    if (error instanceof QuoteTextError) throw error;
    if ((error as Error)?.name === "AbortError") throw new QuoteTextError("Quote writing took too long. Please try again.", 504);
    throw new QuoteTextError("Quote writing is temporarily unavailable.", 502);
  } finally {
    clearTimeout(timer);
  }
}
