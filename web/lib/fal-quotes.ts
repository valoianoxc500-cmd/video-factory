/**
 * The fal calls Quote Studio makes, server-side only.
 *
 * Deliberately small and separate from the Python providers. The pipeline's
 * generators run on the worker where a service account and a GPU are
 * available; Quote Studio runs in a request handler on Vercel, where neither
 * is. What both share is the fal REST API and one credential, so this is the
 * same model and the same published price as the thumbnail path -- reached
 * over HTTP instead of through `core/providers`.
 *
 * `FAL_KEY` is read from the environment on every call and never logged,
 * returned, or embedded in an error message.
 */

const EDIT_URL = "https://fal.run/fal-ai/gemini-25-flash-image/edit";
const LLM_URL = "https://fal.run/fal-ai/any-llm";

/** Long enough for an edit over a large portrait; short enough for a request handler. */
const EDIT_TIMEOUT_MS = 90_000;
const LLM_TIMEOUT_MS = 45_000;

export class FalError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "FalError";
  }
}

function key(): string {
  const k = process.env.FAL_KEY ?? "";
  if (!k) {
    throw new FalError("Image generation is not configured on this server.", 503);
  }
  return k;
}

async function post(
  url: string,
  body: unknown,
  timeoutMs: number,
): Promise<Record<string, unknown>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(url, {
      method: "POST",
      headers: {
        Authorization: `Key ${key()}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(body),
      signal: controller.signal,
    });

    if (!res.ok) {
      // The reason for a 422 is only ever in the body; the status alone makes
      // a validation error and a safety refusal look identical. Truncated so
      // a provider echoing the prompt back cannot flood the response.
      const detail = (await res.text().catch(() => "")).slice(0, 300);
      throw new FalError(
        `Generation failed (${res.status})${detail ? `: ${detail}` : ""}`,
        res.status === 429 ? 429 : 502,
      );
    }
    return (await res.json()) as Record<string, unknown>;
  } catch (err) {
    if (err instanceof FalError) throw err;
    if ((err as Error)?.name === "AbortError") {
      throw new FalError("Generation timed out. Try again.", 504);
    }
    throw new FalError("Could not reach the image service.", 502);
  } finally {
    clearTimeout(timer);
  }
}

/**
 * One slide background, edited from the uploaded portrait.
 *
 * An *edit* rather than a generation, for the same reason the thumbnail path
 * is: asked to draw a named person, a text-to-image model invents someone who
 * merely resembles them. Given the actual photograph it relights and
 * recomposes while the face stays the face it was, which is the whole basis
 * of "the same person on every slide".
 */
export async function editPortrait(
  prompt: string,
  imageDataUri: string,
  aspectRatio: string,
): Promise<string> {
  const body = await post(
    EDIT_URL,
    {
      prompt,
      image_urls: [imageDataUri],
      num_images: 1,
      output_format: "png",
      aspect_ratio: aspectRatio,
    },
    EDIT_TIMEOUT_MS,
  );

  const images = (body.images ?? []) as { url?: string }[];
  const url = images[0]?.url ?? "";
  if (!url) {
    throw new FalError("The image service returned no image.", 502);
  }
  if (url.startsWith("data:")) return url;

  // Fetched server-side and inlined, so the browser never has to reach a
  // third-party CDN to show a slide the user is about to export.
  const fetched = await fetch(url);
  if (!fetched.ok) {
    throw new FalError("Could not download the generated image.", 502);
  }
  const buf = Buffer.from(await fetched.arrayBuffer());
  return `data:image/png;base64,${buf.toString("base64")}`;
}

/** Free-text completion used to write the quotes. Returns raw model text. */
export async function complete(prompt: string): Promise<string> {
  const body = await post(
    LLM_URL,
    { model: "google/gemini-flash-1.5", prompt },
    LLM_TIMEOUT_MS,
  );
  const out = String(body.output ?? "").trim();
  if (!out) throw new FalError("The model returned nothing.", 502);
  return out;
}
