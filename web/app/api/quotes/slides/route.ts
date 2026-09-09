import { requireUser } from "@/lib/supabase/server";
import { rateLimit } from "@/lib/rate-limit";
import { editPortrait, FalError } from "@/lib/fal-quotes";
import { styleById } from "@/lib/quotes";

/**
 * Quote Studio: one slide background.
 *
 * One slide per request, on purpose. It gives the client real progress
 * instead of a spinner, lets a single slide be regenerated without touching
 * the other seven, and keeps every call well inside a serverless timeout --
 * a batch of eight edits would not be.
 *
 * The prompt below forbids text explicitly and repeatedly. An image model
 * asked for a quote returns confident gibberish in Arabic and uneditable
 * lettering in English; the words are composited in the browser instead.
 */

export const dynamic = "force-dynamic";
export const maxDuration = 120;

/** 9:16 for the story format, 4:5 for the in-feed carousel. */
const RATIOS: Record<string, string> = { "9:16": "9:16", "4:5": "4:5" };

const NO_TEXT =
  "ABSOLUTELY NO TEXT of any kind in the image: no words, no letters, no " +
  "Arabic script, no Latin script, no numbers, no captions, no watermark, " +
  "no signature, no logo, no user interface. The image must be purely " +
  "photographic. Any text is a failed result.";

const COMPOSITION =
  "Leave the lower half of the frame visually calm and uncluttered so that " +
  "typography can be placed over it later. Keep the subject's face fully " +
  "visible and in the upper portion of the frame.";

function buildPrompt(styleId: string, variant: number, isOutro: boolean): string {
  const style = styleById(styleId);

  // Variation without identity drift: the pose and framing move, the person
  // does not. Naming the change explicitly is what stops the model treating
  // "another slide" as licence to redraw the face.
  const variations = [
    "Frame as a tight head-and-shoulders portrait, subject facing the camera.",
    "Frame slightly wider, subject turned three-quarters to the camera.",
    "Frame as a medium portrait with the subject looking off-camera.",
    "Frame tightly with dramatic side lighting across the face.",
    "Frame at a low angle, confident and grounded.",
    "Frame with the subject slightly off-centre and generous negative space.",
    "Frame as a clean centred portrait with soft symmetrical light.",
    "Frame as a close portrait with the subject looking directly at the lens.",
  ];

  return [
    "Edit this photograph of a real person into a premium social-media " +
      "portrait slide.",
    "",
    "IDENTITY — the single most important requirement:",
    "Preserve the person's exact face, bone structure, skin tone, hair and " +
      "distinguishing features. This must remain unmistakably the same " +
      "person as the source photograph. Do not beautify, restyle, age, " +
      "slim, or replace the face. Do not substitute a different person.",
    "",
    `STYLE: ${style.prompt}`,
    "",
    `FRAMING: ${isOutro
      ? "Frame as a calm, wide, closing portrait with generous empty space " +
        "for a sign-off."
      : variations[variant % variations.length]}`,
    "",
    COMPOSITION,
    "",
    NO_TEXT,
  ].join("\n");
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();

    // Each slide is a paid generation, so the bucket is per-slide not
    // per-carousel: a stuck client retrying one slide is the realistic risk.
    const gate = rateLimit(`quote-slide:${user.id}`, {
      limit: 40,
      windowSeconds: 120,
    });
    if (!gate.allowed) {
      return Response.json(
        { error: "Too many slides at once. Wait a moment." },
        { status: 429, headers: { "Retry-After": String(gate.retryAfterSeconds) } },
      );
    }

    let body: Record<string, unknown>;
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      return Response.json({ error: "Invalid request body." }, { status: 400 });
    }

    const image = String(body.image ?? "");
    const styleId = String(body.styleId ?? "noir");
    const variant = Number(body.variant ?? 0) || 0;
    const isOutro = Boolean(body.isOutro);
    const ratio = RATIOS[String(body.ratio ?? "9:16")] ?? "9:16";

    if (!image.startsWith("data:image/")) {
      return Response.json(
        { error: "Upload a photo of the person first." },
        { status: 400 },
      );
    }
    // Base64 inflates by ~4/3; this caps the source at roughly 8MB decoded.
    if (image.length > 11_000_000) {
      return Response.json(
        { error: "That photo is too large. Use one under 8MB." },
        { status: 413 },
      );
    }

    const generated = await editPortrait(
      buildPrompt(styleId, variant, isOutro),
      image,
      ratio,
    );

    return Response.json({ image: generated });
  } catch (err) {
    if (err instanceof FalError) {
      return Response.json({ error: err.message }, { status: err.status });
    }
    const message = (err as Error)?.message ?? "Something went wrong.";
    const status = /auth|session|user/i.test(message) ? 401 : 500;
    return Response.json({ error: message }, { status });
  }
}
