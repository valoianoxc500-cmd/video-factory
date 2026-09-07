/**
 * Short-lived signed URLs for private object storage.
 *
 * The bucket holds finished videos and thumbnails. Handing out permanent
 * public URLs would mean anyone who ever saw one keeps access forever, and
 * link-sharing would bypass accounts entirely. Instead the object stays
 * private and each authorized request mints a URL that expires.
 *
 * Credentials never leave the server: the service-account key is read from the
 * environment inside a route handler, and only the finished URL reaches the
 * browser.
 *
 * Signing is done here rather than with @google-cloud/storage to keep the
 * serverless bundle small -- V4 signing is a documented string-to-sign plus an
 * RSA-SHA256 signature, which Node's crypto can do directly.
 */

import { createHash, createSign } from "node:crypto";

/** How long a minted URL stays valid. Long enough to start playback and seek. */
const DEFAULT_TTL_SECONDS = 60 * 15;

export class MediaConfigError extends Error {
  readonly status = 500;
  constructor(message: string) {
    super(message);
    this.name = "MediaConfigError";
  }
}

interface ServiceAccount {
  client_email: string;
  private_key: string;
}

function serviceAccount(): ServiceAccount {
  const raw = process.env.GCS_SERVICE_ACCOUNT_JSON;
  if (!raw) {
    throw new MediaConfigError(
      "Media storage is not configured: set GCS_SERVICE_ACCOUNT_JSON.",
    );
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new MediaConfigError("GCS_SERVICE_ACCOUNT_JSON is not valid JSON.");
  }
  const sa = parsed as Partial<ServiceAccount>;
  if (!sa.client_email || !sa.private_key) {
    throw new MediaConfigError(
      "GCS_SERVICE_ACCOUNT_JSON is missing client_email or private_key.",
    );
  }
  // Vercel env values keep literal \n; PEM parsing needs real newlines.
  return {
    client_email: sa.client_email,
    private_key: sa.private_key.replace(/\\n/g, "\n"),
  };
}

function bucket(): string {
  const name = process.env.GCS_BUCKET;
  if (!name) {
    throw new MediaConfigError("Media storage is not configured: set GCS_BUCKET.");
  }
  return name;
}

/**
 * Reject anything that could reach outside the object it names.
 *
 * Paths come from the database, but a corrupted or hand-edited row must not be
 * able to sign a URL for an arbitrary object, so it is validated at the point
 * of use rather than trusted because of where it came from.
 */
export function assertSafeObjectPath(path: string): string {
  const value = String(path ?? "");
  if (
    !value ||
    value.startsWith("/") ||
    value.includes("..") ||
    value.includes("//") ||
    value.includes("\\") ||
    value.includes("\0") ||
    !/^videos\/[a-z0-9_]+\/[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/.test(value)
  ) {
    throw new MediaConfigError("Refusing to sign an unexpected object path.");
  }
  return value;
}

function encodePath(objectPath: string): string {
  return objectPath
    .split("/")
    .map((segment) => encodeURIComponent(segment))
    .join("/");
}

/** A V4-signed GET URL for one object. */
export function signedUrl(
  objectPath: string,
  ttlSeconds: number = DEFAULT_TTL_SECONDS,
): string {
  const safePath = assertSafeObjectPath(objectPath);
  const { client_email, private_key } = serviceAccount();
  const bucketName = bucket();

  const now = new Date();
  const stamp =
    now.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  const date = stamp.slice(0, 8);
  const scope = `${date}/auto/storage/goog4_request`;
  const expires = Math.min(Math.max(ttlSeconds, 30), 60 * 60 * 24 * 7);

  const canonicalUri = `/${bucketName}/${encodePath(safePath)}`;
  const host = "storage.googleapis.com";

  const params = new URLSearchParams({
    "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
    "X-Goog-Credential": `${client_email}/${scope}`,
    "X-Goog-Date": stamp,
    "X-Goog-Expires": String(expires),
    "X-Goog-SignedHeaders": "host",
  });
  // GCS requires the query string sorted by key for the canonical request.
  const canonicalQuery = [...params.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");

  const canonicalRequest = [
    "GET",
    canonicalUri,
    canonicalQuery,
    `host:${host}`,
    "",
    "host",
    "UNSIGNED-PAYLOAD",
  ].join("\n");

  const hashed = createHashHex(canonicalRequest);
  const stringToSign = [
    "GOOG4-RSA-SHA256",
    stamp,
    scope,
    hashed,
  ].join("\n");

  const signature = createSign("RSA-SHA256")
    .update(stringToSign)
    .sign(private_key, "hex");

  return `https://${host}${canonicalUri}?${canonicalQuery}&X-Goog-Signature=${signature}`;
}

function createHashHex(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

/**
 * A URL for one object, signed when possible.
 *
 * The bucket is still world-readable (roles/storage.objectViewer is granted to
 * allUsers), because removing that before signing works would break every
 * library page with no way to serve media. So while no signing key is
 * configured this falls back to the plain public URL and says so.
 *
 * The fallback retires itself: the moment GCS_SERVICE_ACCOUNT_JSON is set,
 * every response becomes a signed, expiring URL with no code change. Callers
 * get `mode` so the difference is visible rather than silent.
 *
 * Note what this does *not* weaken -- ownership is checked before this is ever
 * called, so the fallback grants nothing that the public bucket does not
 * already grant to anyone with the path.
 */
export function mediaUrl(objectPath: string): {
  url: string;
  mode: "signed" | "public";
  expiresInSeconds: number | null;
} {
  const safePath = assertSafeObjectPath(objectPath);

  if (mediaSigningAvailable()) {
    return {
      url: signedUrl(safePath),
      mode: "signed",
      expiresInSeconds: DEFAULT_TTL_SECONDS,
    };
  }

  const base = process.env.MEDIA_PUBLIC_BASE_URL;
  if (!base) {
    // Neither private nor public storage is configured: say which, rather
    // than returning a broken URL.
    throw new MediaConfigError(
      "Media storage is not configured: set GCS_SERVICE_ACCOUNT_JSON for " +
        "signed URLs, or MEDIA_PUBLIC_BASE_URL while the bucket is public.",
    );
  }

  return {
    url: `${base.replace(/\/$/, "")}/${encodePath(safePath)}`,
    mode: "public",
    expiresInSeconds: null,
  };
}

/** Whether signing is configured, without throwing. */
export function mediaSigningAvailable(): boolean {
  try {
    serviceAccount();
    bucket();
    return true;
  } catch {
    return false;
  }
}
