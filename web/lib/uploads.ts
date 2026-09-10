import { createHash, createSign } from "node:crypto";

/**
 * Direct-to-storage uploads for Clipping.
 *
 * Clipping had no way in. The only route to a clippable video was My Videos,
 * which adds a video by *link* -- and a YouTube link yields metadata with no
 * downloadable file, so the clipping screen was permanently empty. This is the
 * missing half: the browser puts a file the user already has straight into the
 * same bucket the worker reads from.
 *
 * The upload goes browser -> storage, not browser -> server -> storage,
 * because a serverless request body caps out around 4.5MB and a video is not
 * 4.5MB. The server's job is to authorise a single object path and sign a
 * short-lived URL for it; the bytes never touch it.
 *
 * Kept separate from `media.ts` on purpose. That module signs **GET** URLs for
 * finished library videos under `videos/...`; this one signs **PUT** URLs for
 * raw uploads under `vrf/uploads/...`. Sharing one path validator between them
 * would mean widening the one that guards reads, which is the wrong direction.
 */

export class UploadConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UploadConfigError";
  }
}

/** How long a signed upload URL is good for. Long enough for a slow phone. */
const UPLOAD_TTL_SECONDS = 60 * 30;

/**
 * What the backend can actually process, stated so the screen can say it.
 *
 * The worker decodes with ffmpeg, which reads far more than this -- but these
 * are the containers a browser will also preview, and offering a format the
 * user cannot see before clipping is a worse experience than declining it.
 */
export const ACCEPTED_VIDEO_TYPES = [
  "video/mp4",
  "video/quicktime",
  "video/webm",
  "video/x-matroska",
] as const;

export const ACCEPTED_EXTENSIONS = [".mp4", ".mov", ".webm", ".mkv"] as const;

/** 2GB. Above this a browser upload is unreliable enough to be a false promise. */
export const MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024;

export function describeLimits(): string {
  return `MP4, MOV, WebM or MKV · up to ${Math.round(
    MAX_UPLOAD_BYTES / (1024 * 1024 * 1024),
  )}GB`;
}

export function isAcceptedType(contentType: string, filename = ""): boolean {
  const type = String(contentType ?? "").toLowerCase().split(";")[0].trim();
  if ((ACCEPTED_VIDEO_TYPES as readonly string[]).includes(type)) return true;
  // Some browsers send an empty or generic type for .mkv and .mov; fall back
  // to the extension rather than refusing a file we can certainly decode.
  const lower = String(filename ?? "").toLowerCase();
  return (
    (!type || type === "application/octet-stream") &&
    ACCEPTED_EXTENSIONS.some((ext) => lower.endsWith(ext))
  );
}

/**
 * A filename reduced to something safe to put in an object path.
 *
 * The original name is only ever decoration -- the object is addressed by a
 * generated id -- so anything unusual is replaced rather than escaped.
 */
export function safeFilename(raw: string): string {
  const base = String(raw ?? "").split(/[\\/]/).pop() ?? "";
  const cleaned = base.replace(/[^A-Za-z0-9._-]/g, "_").replace(/_{2,}/g, "_");
  const trimmed = cleaned.replace(/^[._-]+/, "").slice(-80);
  return trimmed || "upload.mp4";
}

/** `vrf/uploads/<userId>/<uploadId>/<filename>` and nothing else. */
export function assertUploadPath(path: string): string {
  const value = String(path ?? "");
  if (
    !value ||
    value.startsWith("/") ||
    value.includes("..") ||
    value.includes("//") ||
    value.includes("\\") ||
    value.includes("\0") ||
    !/^vrf\/uploads\/[0-9a-f-]{36}\/[0-9a-f-]{36}\/[A-Za-z0-9._-]{1,80}$/.test(value)
  ) {
    throw new UploadConfigError("Refusing to sign an unexpected object path.");
  }
  return value;
}

export function uploadObjectPath(
  userId: string,
  uploadId: string,
  filename: string,
): string {
  return assertUploadPath(
    `vrf/uploads/${userId}/${uploadId}/${safeFilename(filename)}`,
  );
}

// ── signing ──────────────────────────────────────────────────────────

interface ServiceAccount {
  client_email: string;
  private_key: string;
}

function serviceAccount(): ServiceAccount {
  const raw = process.env.GCS_SERVICE_ACCOUNT_JSON;
  if (!raw) {
    throw new UploadConfigError(
      "Uploads are not configured: set GCS_SERVICE_ACCOUNT_JSON.",
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new UploadConfigError("GCS_SERVICE_ACCOUNT_JSON is not valid JSON.");
  }
  const sa = parsed as Partial<ServiceAccount>;
  if (!sa.client_email || !sa.private_key) {
    throw new UploadConfigError(
      "GCS_SERVICE_ACCOUNT_JSON is missing client_email or private_key.",
    );
  }
  return { client_email: sa.client_email, private_key: sa.private_key };
}

function bucket(): string {
  const name = process.env.GCS_BUCKET;
  if (!name) {
    throw new UploadConfigError("Uploads are not configured: set GCS_BUCKET.");
  }
  return name;
}

export function uploadsAvailable(): boolean {
  try {
    serviceAccount();
    bucket();
    return true;
  } catch {
    return false;
  }
}

function encodePath(objectPath: string): string {
  return objectPath.split("/").map(encodeURIComponent).join("/");
}

/**
 * A V4-signed **PUT** URL for exactly one object.
 *
 * `content-type` is a signed header, so the browser must send the same value
 * it asked for. That is deliberate: it stops a URL minted for an mp4 being
 * reused to write something else to the same path.
 */
export function signedUploadUrl(
  objectPath: string,
  contentType: string,
  ttlSeconds: number = UPLOAD_TTL_SECONDS,
): { url: string; headers: Record<string, string>; expiresInSeconds: number } {
  const safePath = assertUploadPath(objectPath);
  const { client_email, private_key } = serviceAccount();
  const bucketName = bucket();
  const type = String(contentType || "application/octet-stream")
    .toLowerCase()
    .split(";")[0]
    .trim();

  const now = new Date();
  const stamp = now.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  const date = stamp.slice(0, 8);
  const scope = `${date}/auto/storage/goog4_request`;
  const expires = Math.min(Math.max(ttlSeconds, 60), 60 * 60 * 12);

  const canonicalUri = `/${bucketName}/${encodePath(safePath)}`;
  const host = "storage.googleapis.com";

  const params = new URLSearchParams({
    "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
    "X-Goog-Credential": `${client_email}/${scope}`,
    "X-Goog-Date": stamp,
    "X-Goog-Expires": String(expires),
    "X-Goog-SignedHeaders": "content-type;host",
  });
  const canonicalQuery = [...params.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");

  const canonicalRequest = [
    "PUT",
    canonicalUri,
    canonicalQuery,
    `content-type:${type}`,
    `host:${host}`,
    "",
    "content-type;host",
    "UNSIGNED-PAYLOAD",
  ].join("\n");

  const stringToSign = [
    "GOOG4-RSA-SHA256",
    stamp,
    scope,
    createHash("sha256").update(canonicalRequest).digest("hex"),
  ].join("\n");

  const signature = createSign("RSA-SHA256")
    .update(stringToSign)
    .sign(private_key, "hex");

  return {
    url: `https://${host}${canonicalUri}?${canonicalQuery}&X-Goog-Signature=${signature}`,
    headers: { "Content-Type": type },
    expiresInSeconds: expires,
  };
}

/**
 * Where the object will be readable once written.
 *
 * Mirrors `worker/storage.py::_gcs_public_url`, because the worker stores this
 * exact string as an asset's `storage_path` and then downloads it again to
 * process it. The two must agree or an uploaded video is unreachable.
 */
export function publicUploadUrl(objectPath: string): string {
  const safePath = assertUploadPath(objectPath);
  return `https://storage.googleapis.com/${bucket()}/${encodePath(safePath)}`;
}
