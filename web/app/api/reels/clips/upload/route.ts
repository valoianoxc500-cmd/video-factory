import { randomUUID } from "node:crypto";
import { createClient, requireUser } from "@/lib/supabase/server";
import { AssetRepository, toReelsHttpError } from "@/lib/vrf";
import {
  FederationError,
  MAX_UPLOAD_BYTES,
  UploadConfigError,
  isAcceptedType,
  publicUploadUrl,
  safeFilename,
  signedUploadUrl,
  uploadObjectPath,
  uploadsAvailable,
} from "@/lib/uploads";
import { reportFederationFailure } from "@/lib/gcs-auth";

/**
 * Upload a video straight into Clipping.
 *
 * Two steps, because the bytes go browser -> storage and never through here:
 *
 *   sign    authorise one object path and return a short-lived PUT URL
 *   commit  record the uploaded object as an asset, with its rights
 *
 * The rights gate is unchanged and is enforced on `commit`, not on `sign`: a
 * signed URL writes to a path nobody can reach until an asset row points at
 * it, and that row cannot be created without the same attestation My Videos
 * has always required. Uploading is not a way around ownership.
 */

export const dynamic = "force-dynamic";

/** The single confirmation the upload box collects, matching My Videos. */
const RIGHTS_SOURCE = "owned_or_permitted";

function fail(err: unknown) {
  // Both of these are ours, not the customer's. The specific complaint --
  // which variable is unset, which IAM binding Google rejected -- goes to the
  // server log; the customer gets one sentence either way, because there is
  // nothing they can do about a federation binding.
  if (err instanceof FederationError) {
    reportFederationFailure(err);
    return Response.json(
      { error: "Uploads are not available right now." },
      { status: err.isConfig ? 503 : 502 },
    );
  }
  if (err instanceof UploadConfigError) {
    console.error(`[clip-upload] ${err.message}`);
    return Response.json(
      { error: "Uploads are not available right now." },
      { status: 503 },
    );
  }
  const { status, message } = toReelsHttpError(err);
  return Response.json({ error: message }, { status });
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();
    const oidcToken = request.headers.get("x-vercel-oidc-token") ?? undefined;

    let body: Record<string, unknown>;
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      return Response.json({ error: "Invalid request body." }, { status: 400 });
    }

    const action = String(body.action ?? "sign").trim().toLowerCase();

    // ── 1. authorise one object path ──────────────────────────────
    if (action === "sign") {
      if (!uploadsAvailable()) {
        console.error("[clip-upload] storage is not configured");
        return Response.json(
          { error: "Uploads are not available right now." },
          { status: 503 },
        );
      }

      const filename = safeFilename(String(body.filename ?? ""));
      const contentType = String(body.contentType ?? "");
      const size = Number(body.size ?? 0);

      if (!isAcceptedType(contentType, filename)) {
        return Response.json(
          { error: "That file type is not supported. Use MP4, MOV, WebM or MKV." },
          { status: 415 },
        );
      }
      if (!Number.isFinite(size) || size <= 0) {
        return Response.json({ error: "That file looks empty." }, { status: 400 });
      }
      if (size > MAX_UPLOAD_BYTES) {
        return Response.json(
          { error: "That video is larger than 2GB." },
          { status: 413 },
        );
      }

      // The path is derived from the verified session, never from the body, so
      // one user cannot mint a URL that writes into another's prefix.
      const objectPath = uploadObjectPath(user.id, randomUUID(), filename);
      // Awaited now: with no private key available, the V4 signature is
      // produced by Google rather than locally.
      const signed = await signedUploadUrl(
        objectPath,
        contentType || "video/mp4",
        undefined,
        oidcToken,
      );

      return Response.json({
        objectPath,
        uploadUrl: signed.url,
        headers: signed.headers,
        expiresInSeconds: signed.expiresInSeconds,
      });
    }

    // ── 2. record the uploaded object ─────────────────────────────
    if (action === "commit") {
      const objectPath = String(body.objectPath ?? "");
      // Re-derives the prefix from the session: a commit naming somebody
      // else's path fails here rather than attaching their object to this
      // account.
      if (!objectPath.startsWith(`vrf/uploads/${user.id}/`)) {
        return Response.json({ error: "That upload is not yours." }, { status: 403 });
      }
      if (body.ownsOrPermitted !== true) {
        return Response.json(
          {
            error:
              "Confirm you own this video or have permission to use it before clipping it.",
          },
          { status: 400 },
        );
      }

      const url = publicUploadUrl(objectPath);
      const supabase = await createClient();
      const duration = Number(body.durationSeconds ?? 0);

      const asset = await new AssetRepository(supabase).create(user.id, {
        title: String(body.title ?? "").trim().slice(0, 160) || "Uploaded video",
        // The worker downloads `storage_path` directly, and the worker's own
        // uploads store a public URL here -- so this stores the same shape.
        storagePath: url,
        rightsSource: RIGHTS_SOURCE,
        durationSeconds: Number.isFinite(duration) && duration > 0
          ? Math.round(duration * 1000) / 1000
          : null,
      });

      return Response.json({ asset }, { status: 201 });
    }

    return Response.json({ error: "Unknown action." }, { status: 400 });
  } catch (err) {
    return fail(err);
  }
}
