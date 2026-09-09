/**
 * Text-only adapter for Quote Studio's existing model gateway.
 *
 * Quote cards are rendered locally; this call returns words and nothing else.
 * It reaches the same fal credential the rest of the web app already uses --
 * no second provider, no second key.
 *
 * Two rules govern the error handling here, and they pull in opposite
 * directions:
 *
 *   - a customer never sees a provider name, a status code or a response body
 *   - an operator must be able to tell *which* failure happened
 *
 * The first version satisfied only the first rule. Every failure -- a missing
 * credential, a rejected key, a provider outage, a bug in this file --
 * collapsed into one sentence with nothing written anywhere, so a production
 * failure could not be told apart from any other. `report` below is the fix:
 * the detail goes to the server log, the sentence goes to the customer, and
 * the two never swap places.
 */

const TEXT_URL = "https://fal.run/fal-ai/any-llm";
const TIMEOUT_MS = 45_000;

/** Named so a log line can say what to set, without ever printing its value. */
const CREDENTIAL_ENV = "FAL_KEY";

/** The only sentences a customer is ever shown from this module. */
const CUSTOMER = {
  unavailable: "Quote writing is temporarily unavailable.",
  busy: "Quote writing is busy. Please try again shortly.",
  slow: "Quote writing took too long. Please try again.",
  empty: "Quote writing returned no usable text.",
} as const;

export class QuoteTextError extends Error {
  constructor(message: string, readonly status: number) {
    super(message);
    this.name = "QuoteTextError";
  }
}

/**
 * Server-side only. Never returned, never rendered, never thrown onward.
 *
 * `detail` is truncated because a provider that echoes the prompt back would
 * otherwise put the whole request in the log on every failure.
 */
function report(reason: string, detail: unknown = ""): void {
  const text = String(detail ?? "").slice(0, 300);
  console.error(`[quote-text] ${reason}${text ? ` :: ${text}` : ""}`);
}

/** Quote card rendering is local; this call returns text only. */
export async function writeQuoteText(prompt: string): Promise<string> {
  const key = process.env[CREDENTIAL_ENV] ?? "";
  if (!key) {
    // A misconfiguration, not an outage. It reads as "temporarily
    // unavailable" to the customer because there is nothing useful they can
    // do either way -- but it must not read that way in the log, or a
    // permanently broken deploy looks like a passing blip forever.
    report(
      `not configured: ${CREDENTIAL_ENV} is unset in this environment. ` +
        `Quote writing cannot run until it is set.`,
    );
    throw new QuoteTextError(CUSTOMER.unavailable, 503);
  }

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
      // The reason for a 401 or a 422 is only ever in the body; the status
      // alone cannot tell a rejected credential from a rejected request.
      const body = await response.text().catch(() => "");
      report(`provider returned ${response.status}`, body);
      throw new QuoteTextError(
        response.status === 429 ? CUSTOMER.busy : CUSTOMER.unavailable,
        response.status === 429 ? 429 : 502,
      );
    }

    const body = (await response.json()) as Record<string, unknown>;

    // This endpoint answers 200 with a populated `error` field on a refusal
    // or an upstream fault, so a successful status is not by itself a
    // successful generation.
    if (body.error) {
      report("provider returned 200 with an error field", JSON.stringify(body.error));
      throw new QuoteTextError(CUSTOMER.unavailable, 502);
    }

    const text = String(body.output ?? "").trim();
    if (!text) {
      report("provider returned an empty output field", JSON.stringify(Object.keys(body)));
      throw new QuoteTextError(CUSTOMER.empty, 502);
    }
    return text;
  } catch (error) {
    if (error instanceof QuoteTextError) throw error;
    if ((error as Error)?.name === "AbortError") {
      report(`request exceeded ${TIMEOUT_MS}ms`);
      throw new QuoteTextError(CUSTOMER.slow, 504);
    }
    // Anything left is a fault in this file or the runtime -- a DNS failure,
    // a TLS failure, a TypeError. Previously indistinguishable from a
    // provider outage; now it says so.
    report("unexpected failure", (error as Error)?.stack ?? String(error));
    throw new QuoteTextError(CUSTOMER.unavailable, 502);
  } finally {
    clearTimeout(timer);
  }
}
