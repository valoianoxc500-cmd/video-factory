/**
 * OAuth connection for social accounts.
 *
 * The browser starts a connection here and the Python worker later spends the
 * token, so this table is kept in step with `viral/accounts.py`
 * (`OAUTH_PROVIDERS`). `tests/test_viral_web_parity.py` fails if the two ever
 * drift -- a scope missing on one side produces a token that authenticates but
 * cannot publish, which is a miserable bug to find at upload time.
 *
 * No password is ever collected. Connection happens on the platform's own
 * consent screen; this module only builds the URL to send the user to and
 * checks the callback belongs to the same signed-in person who started it.
 */

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";

export type ConnectFlow = "oauth_code" | "oauth_pkce" | "unsupported";

export interface OAuthProvider {
  platform: string;
  flow: ConnectFlow;
  authorizeUrl: string;
  tokenUrl: string;
  scopes: string[];
  clientIdEnv: string;
  clientSecretEnv: string;
  note: string;
}

export const OAUTH_PROVIDERS: Record<string, OAuthProvider> = {
  youtube: {
    platform: "youtube",
    flow: "oauth_code",
    authorizeUrl: "https://accounts.google.com/o/oauth2/v2/auth",
    tokenUrl: "https://oauth2.googleapis.com/token",
    scopes: [
      "https://www.googleapis.com/auth/youtube.upload",
      "https://www.googleapis.com/auth/youtube.readonly",
    ],
    clientIdEnv: "YOUTUBE_OAUTH_CLIENT_ID",
    clientSecretEnv: "YOUTUBE_OAUTH_CLIENT_SECRET",
    note: "",
  },
  instagram: {
    platform: "instagram",
    flow: "oauth_code",
    authorizeUrl: "https://www.facebook.com/v21.0/dialog/oauth",
    tokenUrl: "https://graph.facebook.com/v21.0/oauth/access_token",
    scopes: [
      "instagram_basic",
      "instagram_content_publish",
      "pages_show_list",
      "business_management",
    ],
    clientIdEnv: "META_OAUTH_CLIENT_ID",
    clientSecretEnv: "META_OAUTH_CLIENT_SECRET",
    note: "Requires an Instagram Business or Creator account linked to a Page.",
  },
  facebook: {
    platform: "facebook",
    flow: "oauth_code",
    authorizeUrl: "https://www.facebook.com/v21.0/dialog/oauth",
    tokenUrl: "https://graph.facebook.com/v21.0/oauth/access_token",
    scopes: ["pages_show_list", "pages_manage_posts", "pages_read_engagement"],
    clientIdEnv: "META_OAUTH_CLIENT_ID",
    clientSecretEnv: "META_OAUTH_CLIENT_SECRET",
    note: "",
  },
  tiktok: {
    platform: "tiktok",
    flow: "oauth_pkce",
    authorizeUrl: "https://www.tiktok.com/v2/auth/authorize/",
    tokenUrl: "https://open.tiktokapis.com/v2/oauth/token/",
    scopes: ["user.info.basic", "video.publish", "video.upload"],
    clientIdEnv: "TIKTOK_CLIENT_KEY",
    clientSecretEnv: "TIKTOK_CLIENT_SECRET",
    note: "video.publish requires an audited app; unaudited apps post to drafts.",
  },
  // Threads and X below are connected for Quote Studio's carousel publishing
  // only. They are deliberately absent from `PLATFORMS` in `lib/vrf.ts`,
  // which is what drives the Reels video queue and its adapters -- adding
  // them there would enrol two image-only accounts in a video pipeline that
  // has no idea what to do with them.
  threads: {
    platform: "threads",
    flow: "oauth_code",
    // Threads authenticates on its own hosts, separate from the Facebook
    // dialog Instagram and Facebook use, and separate again from the
    // graph.threads.net host its publishing API lives on.
    authorizeUrl: "https://threads.com/oauth/authorize",
    tokenUrl: "https://graph.threads.com/oauth/access_token",
    scopes: ["threads_basic", "threads_content_publish"],
    clientIdEnv: "THREADS_CLIENT_ID",
    clientSecretEnv: "THREADS_CLIENT_SECRET",
    note:
      "Needs its own Meta Threads app -- the Instagram/Facebook app ID does " +
      "not work here.",
  },
  x: {
    platform: "x",
    flow: "oauth_pkce",
    authorizeUrl: "https://x.com/i/oauth2/authorize",
    tokenUrl: "https://api.x.com/2/oauth2/token",
    // media.write is the v2 scope for uploading images; offline.access is
    // what makes a refresh token be issued at all, and without it the
    // connection dies two hours after it is made.
    scopes: [
      "tweet.read",
      "tweet.write",
      "media.write",
      "users.read",
      "offline.access",
    ],
    clientIdEnv: "X_CLIENT_ID",
    clientSecretEnv: "X_CLIENT_SECRET",
    note: "X carries at most 4 images per post, so it has no true carousel.",
  },
  snapchat: {
    platform: "snapchat",
    flow: "unsupported",
    authorizeUrl: "",
    tokenUrl: "",
    scopes: [],
    clientIdEnv: "",
    clientSecretEnv: "",
    note:
      "Snapchat has no server-side publishing API, so connecting an account " +
      "would not enable anything this product can do.",
  },
};

export class ConnectError extends Error {
  readonly status: number;
  constructor(message: string, status = 400) {
    super(message);
    this.name = "ConnectError";
    this.status = status;
  }
}

export interface AuthSession {
  platform: string;
  userId: string;
  state: string;
  redirectUri: string;
  codeVerifier: string;
  createdAt: string;
}

const STATE_TTL_MINUTES = 15;

/** Cookie holding the in-flight connection's state and PKCE verifier. */
export const STATE_COOKIE = "vrf_oauth";
export const STATE_COOKIE_PATH = "/api/reels/accounts";
export const STATE_COOKIE_MAX_AGE = STATE_TTL_MINUTES * 60;

function base64url(input: Buffer): string {
  return input.toString("base64url");
}

/** The consent-screen URL, plus the state to keep for the callback. */
export function buildAuthorizationUrl(
  platform: string,
  userId: string,
  redirectUri: string,
  now: Date = new Date(),
): { url: string; session: AuthSession } {
  const key = platform.trim().toLowerCase();
  const provider = OAUTH_PROVIDERS[key];
  if (!provider) throw new ConnectError(`Unknown platform '${platform}'.`);
  if (provider.flow === "unsupported") {
    throw new ConnectError(`${key}: ${provider.note}`);
  }
  if (!userId.trim()) {
    throw new ConnectError("Connecting an account requires a signed-in user.", 401);
  }
  const clientId = (process.env[provider.clientIdEnv] ?? "").trim();
  if (!clientId) {
    throw new ConnectError(
      `${key} is not configured. Set ${provider.clientIdEnv} and ` +
        `${provider.clientSecretEnv}.`,
      503,
    );
  }

  let codeVerifier = "";
  let codeChallenge = "";
  if (provider.flow === "oauth_pkce") {
    codeVerifier = base64url(randomBytes(64));
    codeChallenge = base64url(createHash("sha256").update(codeVerifier).digest());
  }

  const session: AuthSession = {
    platform: key,
    userId,
    state: base64url(randomBytes(32)),
    redirectUri,
    codeVerifier,
    createdAt: now.toISOString(),
  };

  const params = new URLSearchParams({
    client_id: clientId,
    redirect_uri: redirectUri,
    response_type: "code",
    scope: provider.scopes.join(" "),
    state: session.state,
  });
  if (key === "youtube") {
    // Without these Google returns no refresh token on a reconnect.
    params.set("access_type", "offline");
    params.set("prompt", "consent");
  }
  if (codeChallenge) {
    params.set("code_challenge", codeChallenge);
    params.set("code_challenge_method", "S256");
  }

  return { url: `${provider.authorizeUrl}?${params.toString()}`, session };
}

/** Check the callback belongs to this user's own in-flight connection. */
export function verifyCallback(
  session: AuthSession | null,
  returnedState: string,
  actingUserId: string,
  now: Date = new Date(),
): AuthSession {
  if (!session) throw new ConnectError("No connection is in progress. Start again.");

  const expected = Buffer.from(session.state);
  const received = Buffer.from(returnedState ?? "");
  const matches =
    expected.length === received.length && timingSafeEqual(expected, received);
  if (!matches) {
    throw new ConnectError("OAuth state did not match. Connection refused.", 400);
  }
  if (session.userId !== actingUserId) {
    throw new ConnectError(
      "This connection was started by a different user; refusing to attach " +
        "the account.",
      403,
    );
  }
  const age = now.getTime() - new Date(session.createdAt).getTime();
  if (age > STATE_TTL_MINUTES * 60 * 1000) {
    throw new ConnectError("The connection attempt expired. Start again.");
  }
  return session;
}

/** Field names that mean someone is trying to hand us a password. */
const CREDENTIAL_FIELDS = new Set([
  "password",
  "passwd",
  "pass",
  "pwd",
  "passphrase",
  "login_password",
  "account_password",
  "social_password",
  "otp",
  "one_time_password",
  "two_factor_code",
  "2fa_code",
  "security_answer",
  "pin",
]);

/** Refuse a connect request carrying platform credentials. */
export function rejectPasswordCredentials(payload: Record<string, unknown>): void {
  const offending = Object.keys(payload ?? {}).filter((k) =>
    CREDENTIAL_FIELDS.has(k.trim().toLowerCase().replace(/-/g, "_")),
  );
  if (offending.length > 0) {
    throw new ConnectError(
      `Refusing credentials in fields [${offending.sort().join(", ")}]. ` +
        "Accounts are connected through the platform's OAuth consent screen; " +
        "this application never asks for, receives or stores a social " +
        "account password.",
    );
  }
}

/** Exchange the authorization code for tokens. */
export async function exchangeCode(
  platform: string,
  code: string,
  session: AuthSession,
): Promise<{
  accessToken: string;
  refreshToken: string;
  expiresIn: number | null;
  scopes: string[];
}> {
  const provider = OAUTH_PROVIDERS[platform];
  if (!provider || provider.flow === "unsupported") {
    throw new ConnectError(`Cannot exchange a code for ${platform}.`);
  }
  const clientId = process.env[provider.clientIdEnv] ?? "";
  const clientSecret = process.env[provider.clientSecretEnv] ?? "";

  const body = new URLSearchParams({
    client_id: clientId,
    client_secret: clientSecret,
    code,
    grant_type: "authorization_code",
    redirect_uri: session.redirectUri,
  });
  if (session.codeVerifier) body.set("code_verifier", session.codeVerifier);

  const response = await fetch(provider.tokenUrl, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  const payload = (await response.json().catch(() => ({}))) as Record<string, unknown>;
  if (!response.ok) {
    const message =
      (payload.error_description as string) ??
      ((payload.error as Record<string, string>)?.message ?? "") ??
      "the platform rejected the connection";
    throw new ConnectError(`Could not connect ${platform}: ${message}`, 502);
  }

  const accessToken = String(
    payload.access_token ?? (payload.data as Record<string, string>)?.access_token ?? "",
  );
  if (!accessToken) {
    throw new ConnectError(`${platform} returned no access token.`, 502);
  }
  const scope = String(payload.scope ?? "");
  return {
    accessToken,
    refreshToken: String(
      payload.refresh_token ??
        (payload.data as Record<string, string>)?.refresh_token ??
        "",
    ),
    expiresIn: Number(payload.expires_in) || null,
    scopes: scope ? scope.split(/[\s,]+/).filter(Boolean) : provider.scopes,
  };
}

/**
 * Which account this token belongs to.
 *
 * `ref` is what the publish adapters address: a YouTube channel id, a Facebook
 * Page id, an Instagram Business account id, a TikTok open_id. Without it a
 * connected account is a token with nowhere to post, so a connection that
 * cannot resolve one is refused rather than stored half-formed.
 */
export async function fetchAccountIdentity(
  platform: string,
  accessToken: string,
): Promise<{ ref: string; handle: string }> {
  const json = async (url: string, init?: RequestInit) => {
    const response = await fetch(url, init);
    return (await response.json().catch(() => ({}))) as Record<string, unknown>;
  };

  if (platform === "youtube") {
    const body = await json(
      "https://www.googleapis.com/youtube/v3/channels?part=snippet&mine=true",
      { headers: { authorization: `Bearer ${accessToken}` } },
    );
    const item = (body.items as Array<Record<string, never>>)?.[0];
    const ref = String(item?.id ?? "");
    const handle = String(
      (item?.snippet as Record<string, string>)?.title ?? "",
    );
    if (!ref) throw new ConnectError("No YouTube channel on that account.", 502);
    return { ref, handle };
  }

  if (platform === "facebook" || platform === "instagram") {
    const body = await json(
      "https://graph.facebook.com/v21.0/me/accounts?fields=id,name," +
        `instagram_business_account{id,username}&access_token=${accessToken}`,
    );
    const pages = (body.data as Array<Record<string, never>>) ?? [];
    if (pages.length === 0) {
      throw new ConnectError(
        "That account manages no Pages. Instagram and Facebook publishing " +
          "needs a Page you administer.",
        400,
      );
    }
    if (platform === "facebook") {
      return {
        ref: String(pages[0].id ?? ""),
        handle: String(pages[0].name ?? ""),
      };
    }
    const withInstagram = pages.find((p) => p.instagram_business_account);
    if (!withInstagram) {
      throw new ConnectError(
        "No Instagram Business account is linked to your Pages. Link one in " +
          "Instagram's settings, then connect again.",
        400,
      );
    }
    const ig = withInstagram.instagram_business_account as Record<string, string>;
    return { ref: String(ig.id ?? ""), handle: String(ig.username ?? "") };
  }

  if (platform === "threads") {
    // Threads publishes and identifies on graph.threads.net, even though its
    // OAuth lives on graph.threads.com. The id returned here is what every
    // later publish call addresses as {threads-user-id}.
    const body = await json(
      "https://graph.threads.net/v1.0/me?fields=id,username" +
        `&access_token=${accessToken}`,
    );
    const ref = String(body.id ?? "");
    if (!ref) {
      throw new ConnectError("That Threads account could not be read.", 502);
    }
    return { ref, handle: String(body.username ?? "") };
  }

  if (platform === "x") {
    const body = await json("https://api.x.com/2/users/me?user.fields=username", {
      headers: { authorization: `Bearer ${accessToken}` },
    });
    const data = (body.data ?? {}) as Record<string, string>;
    const ref = String(data.id ?? "");
    if (!ref) {
      throw new ConnectError("That X account could not be read.", 502);
    }
    return { ref, handle: String(data.username ?? "") };
  }

  if (platform === "tiktok") {
    const body = await json(
      "https://open.tiktokapis.com/v2/user/info/?fields=open_id,display_name",
      { headers: { authorization: `Bearer ${accessToken}` } },
    );
    const user = ((body.data as Record<string, never>)?.user ?? {}) as Record<
      string,
      string
    >;
    const ref = String(user.open_id ?? "");
    if (!ref) throw new ConnectError("TikTok returned no account id.", 502);
    return { ref, handle: String(user.display_name ?? "") };
  }

  throw new ConnectError(`Cannot identify a ${platform} account.`);
}
