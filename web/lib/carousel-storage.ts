import { createHash } from "node:crypto";
import {
  FederationError,
  federationConfig,
  signBlobHex,
} from "./gcs-auth";

/**
 * Hosting for rendered carousel slides.
 *
 * Meta and Threads ingest images **by URL** — they fetch the picture
 * themselves rather than accepting an upload — so a carousel that exists only
 * as canvas bytes in a browser cannot be published at all. This is the bridge:
 * the browser renders the PNGs, puts them straight into GCS under a Quote
 * Studio prefix, and the publisher hands the platforms short-lived signed
 * URLs to read them from.
 *
 * The bucket stays private. These are V4 signed GET URLs with an expiry, not
 * public objects, so a slide stops being fetchable once the publish window
 * closes.
 *
 * Signing is keyless, on the same federated credential Clipping proved: the
 * organisation enforces `iam.disableServiceAccountKeyCreation`, so Google
 * performs the RSA signature via `signBlob` and no private key exists.
 *
 * Deliberately separate from `lib/uploads.ts`. That module guards Clipping's
 * `vrf/` prefix and its validators are shaped for video assets; widening them
 * to also admit `quotes/` objects would mean one validator guarding two
 * products, and the safest version of that is the one that does not exist.
 * The V4 canonical-request construction is restated here rather than shared,
 * and the tests compare both methods' signed strings so the two cannot drift
 * apart silently.
 */

export class CarouselStorageError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CarouselStorageError";
  }
}

/** Long enough to render and upload a full deck from a slow connection. */
const UPLOAD_TTL_SECONDS = 60 * 15;

/**
 * How long a platform has to fetch a slide.
 *
 * Meta fetches during the publish call, so minutes are plenty. Short on
 * purpose: a signed URL that outlives the publish is a private image with a
 * public link.
 */
const READ_TTL_SECONDS = 60 * 20;

export const SLIDE_CONTENT_TYPE = "image/png";

/** Most slides one carousel may host. Above this no platform accepts it anyway. */
export const MAX_SLIDES_PER_PUBLISH = 20;

/** `quotes/<user>/<project>/<publish>/slide-01.png` and nothing else. */
const OBJECT_SHAPE =
  /^quotes\/[0-9a-f-]{36}\/[0-9a-f-]{36}\/[0-9a-f-]{36}\/slide-\d{2}\.png$/;

export function assertSlidePath(path: string): string {
  const value = String(path ?? "");
  if (
    !value ||
    value.startsWith("/") ||
    value.includes("..") ||
    value.includes("//") ||
    value.includes("\\") ||
    value.includes("\0") ||
    !OBJECT_SHAPE.test(value)
  ) {
    throw new CarouselStorageError("Refusing to sign an unexpected object path.");
  }
  return value;
}

/**
 * The object name for one slide.
 *
 * The index is zero-padded because the filename *is* the order: a platform
 * that ingests `slide-10` before `slide-2` would publish the carousel
 * scrambled, and lexical ordering is the cheapest way to make that
 * impossible.
 */
export function slideObjectPath(
  userId: string,
  projectId: string,
  publishId: string,
  index: number,
): string {
  const position = Math.max(1, Math.floor(index) + 1);
  if (position > MAX_SLIDES_PER_PUBLISH) {
    throw new CarouselStorageError("That carousel has too many slides to publish.");
  }
  return assertSlidePath(
    `quotes/${userId}/${projectId}/${publishId}/slide-${String(position).padStart(2, "0")}.png`,
  );
}

/** The slide number a stored object belongs to, for restoring order. */
export function slideIndexOf(path: string): number {
  const found = assertSlidePath(path).match(/slide-(\d{2})\.png$/);
  return found ? Number(found[1]) - 1 : -1;
}

function bucket(): string {
  const name = process.env.GCS_BUCKET;
  if (!name) {
    throw new CarouselStorageError("Publishing storage is not configured: set GCS_BUCKET.");
  }
  return name;
}

export function storageAvailable(): boolean {
  try {
    bucket();
    return federationConfig() !== null;
  } catch {
    return false;
  }
}

function encodePath(objectPath: string): string {
  return objectPath.split("/").map(encodeURIComponent).join("/");
}

/**
 * One V4 signature, either method, always keyless.
 *
 * `oidcToken` is threaded through from the request because Vercel supplies
 * the assertion per request; the credential layer falls back to the
 * environment when it is absent.
 */
async function signV4(
  method: "GET" | "PUT",
  objectPath: string,
  ttlSeconds: number,
  contentType: string | undefined,
  oidcToken: string | undefined,
): Promise<{ url: string; expiresInSeconds: number; type: string }> {
  const config = federationConfig();
  if (!config) {
    throw new CarouselStorageError(
      "Publishing storage is not configured: set GCP_WORKLOAD_IDENTITY_PROVIDER " +
        "and GCP_SERVICE_ACCOUNT_EMAIL.",
    );
  }

  const bucketName = bucket();
  const type = String(contentType || SLIDE_CONTENT_TYPE).toLowerCase().split(";")[0].trim();
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, "");
  const scope = `${stamp.slice(0, 8)}/auto/storage/goog4_request`;
  const expires = Math.min(Math.max(ttlSeconds, 60), 60 * 60 * 12);

  const host = "storage.googleapis.com";
  const canonicalUri = `/${bucketName}/${encodePath(objectPath)}`;
  const signedHeaders = method === "PUT" ? "content-type;host" : "host";

  const params = new URLSearchParams({
    "X-Goog-Algorithm": "GOOG4-RSA-SHA256",
    "X-Goog-Credential": `${config.serviceAccount}/${scope}`,
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

  const signature = await signBlobHex(config, stringToSign, oidcToken);
  return {
    url: `https://${host}${canonicalUri}?${canonicalQuery}&X-Goog-Signature=${signature}`,
    expiresInSeconds: expires,
    type,
  };
}

/** A signed PUT URL the browser uses to place one rendered slide. */
export async function signedSlideUpload(
  objectPath: string,
  oidcToken?: string,
): Promise<{ url: string; headers: Record<string, string>; expiresInSeconds: number }> {
  const safePath = assertSlidePath(objectPath);
  const signed = await signV4("PUT", safePath, UPLOAD_TTL_SECONDS, SLIDE_CONTENT_TYPE, oidcToken);
  return {
    url: signed.url,
    headers: { "Content-Type": signed.type },
    expiresInSeconds: signed.expiresInSeconds,
  };
}

/**
 * Signed GET URLs for a whole carousel, in slide order.
 *
 * Order is restored from the filenames rather than trusted from the caller:
 * the sequence is the one thing a carousel cannot get wrong, and the object
 * names already encode it.
 */
export async function signedSlideReads(
  objectPaths: string[],
  oidcToken?: string,
): Promise<{ urls: string[]; expiresInSeconds: number }> {
  const ordered = [...(objectPaths ?? [])]
    .map((path) => ({ path: assertSlidePath(path), index: slideIndexOf(path) }))
    .sort((a, b) => a.index - b.index);

  if (!ordered.length) {
    throw new CarouselStorageError("There are no slides to publish.");
  }

  const urls: string[] = [];
  let expiresInSeconds = READ_TTL_SECONDS;
  for (const item of ordered) {
    const signed = await signV4("GET", item.path, READ_TTL_SECONDS, undefined, oidcToken);
    urls.push(signed.url);
    expiresInSeconds = signed.expiresInSeconds;
  }
  return { urls, expiresInSeconds };
}

/** Diagnostics for a storage failure. Server-side only. */
export function reportStorageFailure(error: unknown): void {
  if (error instanceof FederationError || error instanceof CarouselStorageError) {
    console.error(`[carousel-storage] ${error.message}`);
    return;
  }
  console.error(
    `[carousel-storage] unexpected :: ${String((error as Error)?.message ?? error).slice(0, 300)}`,
  );
}
