import { cookies } from "next/headers";
import { requireUser } from "@/lib/supabase/server";
import { toReelsHttpError } from "@/lib/vrf";
import {
  STATE_COOKIE,
  STATE_COOKIE_MAX_AGE,
  STATE_COOKIE_PATH,
  buildAuthorizationUrl,
  rejectPasswordCredentials,
} from "@/lib/vrf-oauth";
import { tokenKeyConfigured } from "@/lib/vrf-crypto";

/**
 * Start an OAuth connection.
 *
 * The user is sent to the platform's own consent screen; no credential is
 * collected here. The state and PKCE verifier are held in a short-lived
 * httpOnly cookie and checked at the callback against the signed-in user, so
 * one person's callback cannot attach an account to somebody else's row.
 */

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  try {
    const user = await requireUser();
    const url = new URL(request.url);
    const platform = (url.searchParams.get("platform") ?? "").toLowerCase();

    // Nothing about this flow should ever carry a password; if a caller has
    // built one that does, stop rather than ignore it.
    rejectPasswordCredentials(Object.fromEntries(url.searchParams.entries()));

    if (!tokenKeyConfigured()) {
      return Response.json(
        {
          error:
            "Token encryption is not configured on this deployment, so " +
            "accounts cannot be connected yet.",
        },
        { status: 503 },
      );
    }

    const redirectUri = `${url.origin}/api/reels/accounts/callback`;
    const { url: authorizeUrl, session } = buildAuthorizationUrl(
      platform,
      user.id,
      redirectUri,
    );

    const store = await cookies();
    store.set(STATE_COOKIE, JSON.stringify(session), {
      httpOnly: true,
      secure: process.env.NODE_ENV === "production",
      sameSite: "lax",
      path: STATE_COOKIE_PATH,
      maxAge: STATE_COOKIE_MAX_AGE,
    });

    return Response.redirect(authorizeUrl, 302);
  } catch (err) {
    const { status, message } = toReelsHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
