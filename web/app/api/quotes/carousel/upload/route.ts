import { randomUUID } from "node:crypto";
import { requireUser } from "@/lib/supabase/server";
import { rateLimit } from "@/lib/rate-limit";
import {
  CarouselStorageError,
  MAX_SLIDES_PER_PUBLISH,
  SLIDE_CONTENT_TYPE,
  reportStorageFailure,
  signedSlideUpload,
  slideObjectPath,
  storageAvailable,
} from "@/lib/carousel-storage";
import { FederationError } from "@/lib/gcs-auth";

/**
 * Signed PUT URLs for one carousel's rendered slides.
 *
 * The browser draws the PNGs and puts them straight into GCS; the bytes never
 * pass through here. This route's whole job is to authorise a specific set of
 * object paths under this user's own prefix, and to refuse everything else.
 *
 * The paths are **derived from the verified session**, never taken from the
 * request. A client can ask for "eight slides for project X" but cannot ask
 * for a path — so no request, however malformed or malicious, can mint a URL
 * that writes into another user's prefix or outside `quotes/`.
 *
 * The OIDC assertion is read from the request header and passed explicitly to
 * the signer, which is the plumbing Clipping already proved in production.
 * It is never logged.
 */

export const dynamic = "force-dynamic";

function fail(err: unknown) {
  // Both cases are ours, not the customer's. The specific complaint goes to
  // the server log; the customer gets one sentence, because a federation
  // binding is not something they can act on.
  if (err instanceof FederationError || err instanceof CarouselStorageError) {
    reportStorageFailure(err);
    return Response.json(
      { error: "Publishing is not available right now." },
      { status: 503 },
    );
  }
  const message = (err as Error)?.message ?? "";
  if (/auth|session|user/i.test(message)) {
    return Response.json({ error: "You must be signed in." }, { status: 401 });
  }
  console.error(`[carousel-upload] ${message.slice(0, 300)}`);
  return Response.json({ error: "Could not prepare the upload." }, { status: 500 });
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();
    const oidcToken = request.headers.get("x-vercel-oidc-token") ?? undefined;

    const gate = rateLimit(`carousel-upload:${user.id}`, {
      limit: 60,
      windowSeconds: 120,
    });
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

    if (!storageAvailable()) {
      console.error("[carousel-upload] storage or federation is not configured");
      return Response.json(
        { error: "Publishing is not available right now." },
        { status: 503 },
      );
    }

    const projectId = String(body.projectId ?? "").trim();
    // A uuid, because it becomes a path segment. Anything else is refused
    // before it can reach the signer.
    if (!/^[0-9a-f-]{36}$/i.test(projectId)) {
      return Response.json({ error: "Save the carousel first." }, { status: 400 });
    }

    const count = Number(body.count ?? 0);
    if (!Number.isInteger(count) || count < 1) {
      return Response.json({ error: "There are no slides to upload." }, { status: 400 });
    }
    if (count > MAX_SLIDES_PER_PUBLISH) {
      return Response.json(
        { error: "That carousel has too many slides to publish." },
        { status: 400 },
      );
    }

    const contentType = String(body.contentType ?? SLIDE_CONTENT_TYPE)
      .toLowerCase()
      .split(";")[0]
      .trim();
    if (contentType !== SLIDE_CONTENT_TYPE) {
      return Response.json(
        { error: "Slides must be PNG images." },
        { status: 415 },
      );
    }

    // One publish id groups this attempt's objects, so a retry writes a fresh
    // set rather than overwriting slides a provider may still be fetching.
    const publishId = randomUUID();

    const slides = [];
    for (let index = 0; index < count; index++) {
      const objectPath = slideObjectPath(user.id, projectId, publishId, index);
      const signed = await signedSlideUpload(objectPath, oidcToken);
      slides.push({
        // The object path is returned because the publish call needs it back.
        // It names an object in a private bucket and grants nothing on its
        // own -- reading it still requires a separately signed URL.
        objectPath,
        uploadUrl: signed.url,
        headers: signed.headers,
      });
    }

    return Response.json({ publishId, slides });
  } catch (err) {
    return fail(err);
  }
}
