import { createHash } from "node:crypto";

/**
 * Keyless Google credentials for Clipping uploads.
 *
 * The organisation enforces `iam.disableServiceAccountKeyCreation`, so there
 * is no JSON key to sign with and there never will be. That rules out the
 * usual approach — hold a private key, sign a V4 URL locally — and it rules
 * it out permanently rather than temporarily.
 *
 * The replacement is the path Google supports for exactly this case:
 *
 *   1. Vercel mints an OIDC token for the running deployment
 *   2. Google STS exchanges it for a federated token   (Workload Identity)
 *   3. IAM Credentials mints an access token for the service account
 *   4. IAM Credentials **signs the V4 string-to-sign** for us
 *
 * Step 4 is the part that makes this work at all. `signBlob` performs the RSA
 * signature server-side, using a key Google holds and nobody can export, so a
 * V4 signed upload URL can still be produced with no private key in this
 * process, no key in the environment, and no key in existence.
 *
 * The browser still uploads straight to GCS. Nothing here proxies bytes; a
 * 2GB file would not survive a serverless request body, and the whole point
 * of a signed URL is that it does not have to.
 *
 * Kept out of `media.ts` deliberately: that module signs GET URLs for the
 * library and still uses the local key path. This is Clipping's upload
 * credential and nothing else's.
 */

const STS_URL = "https://sts.googleapis.com/v1/token";
const IAM_CREDENTIALS = "https://iamcredentials.googleapis.com/v1";
const SCOPE = "https://www.googleapis.com/auth/cloud-platform";

/** Federation calls are small; a long timeout here only hides a broken setup. */
const AUTH_TIMEOUT_MS = 10_000;

/**
 * Refresh this long before expiry. An access token that dies mid-request
 * produces a 401 the customer reads as "upload broken".
 */
const EXPIRY_SKEW_MS = 60_000;

export class FederationError extends Error {
  constructor(
    message: string,
    /** True when the cause is configuration, not a transient fault. */
    readonly isConfig: boolean = false,
  ) {
    super(message);
    this.name = "FederationError";
  }
}

/**
 * Server-side only. Never returned to a caller, never rendered.
 *
 * Tokens and assertions are never logged — only their absence, their length,
 * or the provider's own error text, which describes the misconfiguration
 * without containing a credential.
 */
function report(reason: string, detail: unknown = ""): void {
  const text = String(detail ?? "").slice(0, 300);
  console.error(`[gcs-auth] ${reason}${text ? ` :: ${text}` : ""}`);
}

// ── configuration ────────────────────────────────────────────────────

export interface FederationConfig {
  /** `projects/<number>/locations/global/workloadIdentityPools/<pool>/providers/<provider>` */
  provider: string;
  /** The service account to impersonate and sign as. */
  serviceAccount: string;
}

/**
 * Read the federation settings, or explain precisely what is missing.
 *
 * Both variables are named in the error because "auth failed" on a deploy you
 * cannot attach a debugger to is not an actionable message.
 */
export function federationConfig(): FederationConfig | null {
  const provider = (process.env.GCP_WORKLOAD_IDENTITY_PROVIDER ?? "").trim();
  const serviceAccount = (process.env.GCP_SERVICE_ACCOUNT_EMAIL ?? "").trim();

  if (!provider && !serviceAccount) return null;

  if (!provider) {
    throw new FederationError(
      "GCP_WORKLOAD_IDENTITY_PROVIDER is not set. It must be the full " +
        "resource name: projects/<number>/locations/global/" +
        "workloadIdentityPools/<pool>/providers/<provider>.",
      true,
    );
  }
  if (!serviceAccount) {
    throw new FederationError(
      "GCP_SERVICE_ACCOUNT_EMAIL is not set. It must be the service account " +
        "to impersonate, e.g. name@project.iam.gserviceaccount.com.",
      true,
    );
  }
  if (!/^projects\/\d+\/locations\/global\/workloadIdentityPools\/[^/]+\/providers\/[^/]+$/.test(provider)) {
    throw new FederationError(
      "GCP_WORKLOAD_IDENTITY_PROVIDER is not a valid provider resource name. " +
        "Expected projects/<number>/locations/global/workloadIdentityPools/" +
        "<pool>/providers/<provider>.",
      true,
    );
  }
  if (!/^[^@\s]+@[^@\s]+\.iam\.gserviceaccount\.com$/.test(serviceAccount)) {
    throw new FederationError(
      "GCP_SERVICE_ACCOUNT_EMAIL is not a service account address.",
      true,
    );
  }
  return { provider, serviceAccount };
}

/** Whether the keyless path is configured at all. */
export function federationAvailable(): boolean {
  try {
    return federationConfig() !== null;
  } catch {
    // Present but malformed still counts as "intended"; the caller surfaces
    // the specific complaint rather than silently falling back to a key.
    return true;
  }
}

/**
 * The deployment's own OIDC assertion.
 *
 * Vercel injects this per request when OIDC federation is enabled for the
 * project. Its absence is the single most likely cause of a failure here and
 * is reported as its own case for that reason.
 */
function vercelOidcToken(explicitToken?: string): string {
  const token = String(explicitToken ?? process.env.VERCEL_OIDC_TOKEN ?? "").trim();
  if (!token) {
    throw new FederationError(
      "VERCEL_OIDC_TOKEN is not present. Enable OIDC federation for this " +
        "Vercel project (Settings -> Security -> OIDC Federation) and redeploy.",
      true,
    );
  }
  return token;
}

// ── token exchange ───────────────────────────────────────────────────

async function post(
  url: string,
  body: unknown,
  headers: Record<string, string> = {},
  sensitiveValues: string[] = [],
): Promise<Record<string, unknown>> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), AUTH_TIMEOUT_MS);
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await response.text();
    if (!response.ok) {
      // Google's federation errors name the exact mapping or binding that is
      // wrong, which is the whole value of logging them.
      const safeText = sensitiveValues.reduce(
        (value, secret) => secret ? value.split(secret).join("[redacted]") : value,
        text,
      ).slice(0, 300);
      throw new FederationError(
        `${new URL(url).hostname} returned ${response.status}: ${safeText}`,
        response.status === 400 || response.status === 403,
      );
    }
    return text ? (JSON.parse(text) as Record<string, unknown>) : {};
  } catch (error) {
    if (error instanceof FederationError) throw error;
    if ((error as Error)?.name === "AbortError") {
      throw new FederationError("Google authentication timed out.");
    }
    throw new FederationError("Could not reach Google authentication.");
  } finally {
    clearTimeout(timer);
  }
}

interface CachedToken {
  token: string;
  expiresAt: number;
  assertionFingerprint: string;
}

/**
 * Module-scoped cache.
 *
 * A serverless instance serves many requests, and the STS + impersonation
 * round trip is two network calls. Caching until shortly before expiry turns
 * that into two calls per instance rather than two per upload.
 */
let cached: CachedToken | null = null;

/** An access token for the configured service account. Cached until near expiry. */
export async function serviceAccountAccessToken(
  config: FederationConfig,
  explicitOidcToken?: string,
): Promise<string> {
  const assertion = vercelOidcToken(explicitOidcToken);
  const assertionFingerprint = createHash("sha256").update(assertion).digest("hex");
  if (
    cached &&
    cached.assertionFingerprint === assertionFingerprint &&
    cached.expiresAt - EXPIRY_SKEW_MS > Date.now()
  ) {
    return cached.token;
  }

  // 1. OIDC assertion -> federated token
  const sts = await post(STS_URL, {
    audience: `//iam.googleapis.com/${config.provider}`,
    grantType: "urn:ietf:params:oauth:grant-type:token-exchange",
    requestedTokenType: "urn:ietf:params:oauth:token-type:access_token",
    scope: SCOPE,
    subjectTokenType: "urn:ietf:params:oauth:token-type:jwt",
    subjectToken: assertion,
  }, {}, [assertion]);
  const federated = String(sts.access_token ?? "");
  if (!federated) {
    throw new FederationError("Google returned no federated token.");
  }

  // 2. Federated token -> service account access token
  const impersonated = await post(
    `${IAM_CREDENTIALS}/projects/-/serviceAccounts/${encodeURIComponent(
      config.serviceAccount,
    )}:generateAccessToken`,
    { scope: [SCOPE], lifetime: "3600s" },
    { Authorization: `Bearer ${federated}` },
    [federated],
  );
  const token = String(impersonated.accessToken ?? "");
  if (!token) {
    throw new FederationError("Google returned no access token.");
  }

  const expiry = Date.parse(String(impersonated.expireTime ?? ""));
  cached = {
    token,
    expiresAt: Number.isFinite(expiry) ? expiry : Date.now() + 55 * 60 * 1000,
    assertionFingerprint,
  };
  return token;
}

/**
 * Sign a V4 string-to-sign with the service account's Google-held key.
 *
 * Returns a lowercase hex signature, which is the encoding a V4 signed URL
 * requires. `signBlob` answers in base64, so it is converted here rather than
 * at the call site — a base64 signature in the query string produces a URL
 * that looks correct and is rejected.
 */
export async function signBlobHex(
  config: FederationConfig,
  stringToSign: string,
  explicitOidcToken?: string,
): Promise<string> {
  const accessToken = await serviceAccountAccessToken(config, explicitOidcToken);
  const response = await post(
    `${IAM_CREDENTIALS}/projects/-/serviceAccounts/${encodeURIComponent(
      config.serviceAccount,
    )}:signBlob`,
    { payload: Buffer.from(stringToSign, "utf8").toString("base64") },
    { Authorization: `Bearer ${accessToken}` },
    [accessToken],
  );

  const signed = String(response.signedBlob ?? "");
  if (!signed) {
    throw new FederationError("Google returned no signature.");
  }
  return Buffer.from(signed, "base64").toString("hex");
}

/** Test seam: drop the cached token so a test can observe a fresh exchange. */
export function resetTokenCache(): void {
  cached = null;
}

/** Diagnostics for a failure, logged and never shown to a customer. */
export function reportFederationFailure(error: unknown): void {
  if (error instanceof FederationError) {
    report(error.isConfig ? "configuration" : "transient", error.message);
    return;
  }
  report("unexpected", (error as Error)?.message ?? String(error));
}
