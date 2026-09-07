import { cookies } from "next/headers";
import { createClient, requireUser } from "@/lib/supabase/server";
import { AccountRepository, toReelsHttpError } from "@/lib/vrf";
import {
  ConnectError,
  STATE_COOKIE,
  exchangeCode,
  fetchAccountIdentity,
  verifyCallback,
  type AuthSession,
} from "@/lib/vrf-oauth";
import { encryptToken } from "@/lib/vrf-crypto";

/**
 * The OAuth callback.
 *
 * The state cookie proves this callback belongs to the flow this same user
 * started. The tokens are encrypted before they touch the database, bound to
 * this user and platform, and the plaintext never leaves this function.
 */

export const dynamic = "force-dynamic";
export const maxDuration = 30;

function back(origin: string, params: Record<string, string>) {
  const url = new URL("/dashboard/reels/accounts", origin);
  for (const [key, value] of Object.entries(params)) {
    url.searchParams.set(key, value);
  }
  return Response.redirect(url.toString(), 303);
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const store = await cookies();

  try {
    const user = await requireUser();

    const denied = url.searchParams.get("error");
    if (denied) {
      return back(url.origin, {
        error: "Connection cancelled on the platform's consent screen.",
      });
    }

    const raw = store.get(STATE_COOKIE)?.value ?? "";
    let session: AuthSession | null = null;
    try {
      session = raw ? (JSON.parse(raw) as AuthSession) : null;
    } catch {
      session = null;
    }

    const verified = verifyCallback(
      session,
      url.searchParams.get("state") ?? "",
      user.id,
    );

    const code = url.searchParams.get("code") ?? "";
    if (!code) throw new ConnectError("The platform returned no authorization code.");

    const tokens = await exchangeCode(verified.platform, code, verified);
    const identity = await fetchAccountIdentity(
      verified.platform,
      tokens.accessToken,
    );

    const supabase = await createClient();
    await new AccountRepository(supabase).upsert(user.id, {
      platform: verified.platform,
      accountHandle: identity.handle,
      accountRef: identity.ref,
      scopes: tokens.scopes,
      accessTokenEncrypted: encryptToken(
        tokens.accessToken,
        user.id,
        verified.platform,
      ),
      refreshTokenEncrypted: tokens.refreshToken
        ? encryptToken(tokens.refreshToken, user.id, verified.platform)
        : "",
      expiresAt: tokens.expiresIn
        ? new Date(Date.now() + tokens.expiresIn * 1000).toISOString()
        : null,
    });

    return back(url.origin, { connected: verified.platform });
  } catch (err) {
    const { message } = toReelsHttpError(err);
    return back(url.origin, { error: message });
  } finally {
    // One-shot: the state must not survive to be replayed.
    store.delete(STATE_COOKIE);
  }
}
