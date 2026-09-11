import { createHash, createSign } from "node:crypto";
import {
  FederationError,
  federationAvailable,
  federationConfig,
  signBlobHex,
} from "./gcs-auth";

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
 *
 * Signing is keyless in production. The organisation enforces
 * `iam.disableServiceAccountKeyCreation`, so there is no private key to sign
 * with; `gcs-auth` federates the deployment's OIDC token and has Google sign
 * the V4 string on the service account's behalf. A local JSON key is still
 * honoured when one is present, purely so development keeps working -- it is
 * the fallback now, not the path.
 */

export { FederationError };

export class UploadConfigError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "UploadConfigError";
  }
}

/** How long a signed upload URL is good for. Long enough for a slow phone. */
const UPLOAD_TTL_SECONDS = 60 * 30;

/** Read URLs are for one viewing session, not for sharing. */
const READ_TTL_SECONDS = 60 * 60;

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

interface ServiceAccountKey {
  client_email: string;
  private_key: string;
}

/**
 * A local JSON key, when one exists.
 *
 * Returns null rather than throwing when the variable is absent: absence is
 * the normal production state now, not an error. A *malformed* key is still
 * an error, because it means somebody meant to configure one.
 */
function localKey(): ServiceAccountKey | null {
  const raw = (process.env.GCS_SERVICE_ACCOUNT_JSON ?? "").trim();
  if (!raw) return null;

  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new UploadConfigError("GCS_SERVICE_ACCOUNT_JSON is not valid JSON.");
  }
  const sa = parsed as Partial<ServiceAccountKey>;
  if (!sa.client_email || !sa.private_key) {
    throw new UploadConfigError(
      "GCS_SERVICE_ACCOUNT_JSON is missing client_email or private_key.",
    );
  }
  return { client_email: sa.client_email, private_key: sa.private_key };
}

/**
 * Which credential will sign, and as whom.
 *
 * Federation is preferred whenever it is configured, so a stray key left in a
 * development environment cannot quietly become the production path.
 */
export function signerIdentity(): { mode: "federated" | "key"; email: string } {
  if (federationAvailable()) {
    const config = federationConfig();
    if (config) return { mode: "federated", email: config.serviceAccount };
  }
  const key = localKey();
  if (key) return { mode: "key", email: key.client_email };
  throw new UploadConfigError(
    "Uploads are not configured. Set GCP_WORKLOAD_IDENTITY_PROVIDER and " +
      "GCP_SERVICE_ACCOUNT_EMAIL for keyless signing, or " +
      "GCS_SERVICE_ACCOUNT_JSON for local development.",
  );
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
    signerIdentity();
    bucket();
    return true;
  } catch {
    return false;
  }
}

/**
 * Object paths Clipping is allowed to read.
 *
 * Wider than the upload validator because Clipping reads three shapes the
 * worker also writes:
 *
 *   vrf/uploads/<user>/<upload>/<file>   what the browser uploaded
 *   vrf/<user>/<asset>/source.mp4        what the worker imported
 *   vrf/<user>/<asset>/processed.mp4     the finished clip
 *
 * Still confined to the `vrf/` prefix, so a signed read can never be minted
 * for the library's `videos/...` objects. Ownership is checked separately, by
 * the route, against the database -- this only bounds the *shape*.
 */
export function assertClipReadPath(path: string): string {
  const value = String(path ?? "");
  const shapeOk =
    /^vrf\/uploads\/[0-9a-f-]{36}\/[0-9a-f-]{36}\/[A-Za-z0-9._-]{1,80}$/.test(value) ||
    /^vrf\/[0-9a-f-]{36}\/[0-9a-f-]{36}\/[A-Za-z0-9._-]{1,80}$/.test(value);
  if (
    !value ||
    value.startsWith("/") ||
    value.includes("..") ||
    value.includes("//") ||
    value.includes("\\") ||
    value.includes("\0") ||
    !shapeOk
  ) {
    throw new UploadConfigError("Refusing to sign an unexpected object path.");
  }
  return value;
}

/**
 * The object path behind a stored `storage_path` / `processed_path`.
 *
 * Those columns hold a full public URL, because that is what the worker
 * writes and what the worker downloads again. To sign a read we need the
 * object name back, and it must be confirmed to belong to *our* bucket --
 * otherwise a tampered row could aim a signed URL at somebody else's object.
 */
export function objectPathFromStored(stored: string): string {
  const value = String(stored ?? "").trim();
  if (!value) throw new UploadConfigError("That video has no stored file.");

  let path = value;
  if (/^https?:\/\//i.test(value)) {
    let parsed: URL;
    try {
      parsed = new URL(value);
    } catch {
      throw new UploadConfigError("Refusing to sign an unexpected object path.");
    }
    if (parsed.hostname !== "storage.googleapis.com") {
      throw new UploadConfigError("Refusing to sign an unexpected object path.");
    }
    const prefix = `/${bucket()}/`;
    if (!parsed.pathname.startsWith(prefix)) {
      throw new UploadConfigError("Refusing to sign an unexpected object path.");
    }
    path = decodeURIComponent(parsed.pathname.slice(prefix.length));
  }
  return assertClipReadPath(path);
}

function encodePath(objectPath: string): string {
  return objectPath.split("/").map(encodeURIComponent).join("/");
}

/**
 * One V4 signature, either method, with whichever credential is configured.
 *
 * The credential is the only thing that differs between production and a
 * developer's laptop: federated signing hands the string to Google and gets
 * the signature back, so nothing in this process ever holds a private key.
 */
async function signV4(
  method: "GET" | "PUT",
  objectPath: string,
  ttlSeconds: number,
  contentType?: string,
  oidcToken?: string,
): Promise<{ url: string; expiresInSeconds: number; type: string }> {
  const signer = signerIdentity();
  const bucketName = bucket();
  const type = String(contentType || "application/octet-stream")
    .toLowerCase()
    .split(";")[0]
    .trim();

  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  const scope = `${stamp.slice(0, 8)}/auto/storage/goog4_request`;
  const expires = Math.min(Math.max(ttlSeconds, 60), 60 * 60 * 12);

  const host = "storage.googleapis.com";
  const canonicalUri = `/${bucketName}/${encodePath(objectPath)}`;
  // A PUT binds the content type so a URL minted for an mp4 cannot be reused
  // to write something else; a GET has no body and signs only the host.
  const signedHeaders = method === "PUT" ? "content-type;host" : "host";

  const params = new URLSearchParams({
    "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
    "X-Goog-Credential": `${signer.email}/${scope}`,
    "X-Goog-Date": stamp,
    "X-Goog-Expires": String(expires),
    "X-Goog-SignedHeaders": signedHeaders,
  });
  const canonicalQuery = [...params.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");

  const canonicalRequest = [
    method,
    canonicalUri,
    canonicalQuery,
    ...(method === "PUT" ? [`content-type:${type}`] : []),
    `host:${host}`,
    "",
    signedHeaders,
    "UNSIGNED-PAYLOAD",
  ].join("\n");

  const stringToSign = [
    "GOOG4-RSA-SHA256",
    stamp,
    scope,
    createHash("sha256").update(canonicalRequest).digest("hex"),
  ].join("\n");

  let signature: string;
  if (signer.mode === "federated") {
    const config = federationConfig();
    if (!config) {
      throw new UploadConfigError("Workload identity federation is not configured.");
    }
    signature = await signBlobHex(config, stringToSign, oidcToken);
  } else {
    const key = localKey();
    if (!key) {
      throw new UploadConfigError("No local signing key is available.");
    }
    signature = createSign("RSA-SHA256").update(stringToSign).sign(key.private_key, "hex");
  }

  return {
    url: `https://${host}${canonicalUri}?${canonicalQuery}&X-Goog-Signature=${signature}`,
    expiresInSeconds: expires,
    type,
  };
}

/**
 * A V4-signed **PUT** URL for exactly one object.
 *
 * `content-type` is a signed header, so the browser must send the same value
 * it asked for. That is deliberate: it stops a URL minted for an mp4 being
 * reused to write something else to the same path.
 */
export async function signedUploadUrl(
  objectPath: string,
  contentType: string,
  ttlSeconds: number = UPLOAD_TTL_SECONDS,
  oidcToken?: string,
): Promise<{ url: string; headers: Record<string, string>; expiresInSeconds: number }> {
  const safePath = assertUploadPath(objectPath);
  const signed = await signV4("PUT", safePath, ttlSeconds, contentType, oidcToken);
  return {
    url: signed.url,
    headers: { "Content-Type": signed.type },
    expiresInSeconds: signed.expiresInSeconds,
  };
}

/**
 * A V4-signed **GET** URL for one Clipping object.
 *
 * Short-lived on purpose: this is what the preview player and the download
 * button use, so a link copied out of the page stops working rather than
 * granting permanent access to a customer's footage.
 */
export async function signedReadUrl(
  storedPathOrUrl: string,
  ttlSeconds: number = READ_TTL_SECONDS,
  oidcToken?: string,
): Promise<{ url: string; expiresInSeconds: number }> {
  const objectPath = objectPathFromStored(storedPathOrUrl);
  const signed = await signV4("GET", objectPath, ttlSeconds, undefined, oidcToken);
  return { url: signed.url, expiresInSeconds: signed.expiresInSeconds };
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
