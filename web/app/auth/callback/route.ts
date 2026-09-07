/**
 * OAuth callback.
 *
 * Google sends the browser back here with a one-time `code`. Exchanging it for
 * a session has to happen in a Route Handler rather than a Server Component,
 * because only a handler may write cookies -- and the session *is* the cookies.
 *
 * The exchange is server-side, so the code never round-trips through client
 * JavaScript, and no Google credential is involved here at all: Supabase holds
 * the client secret and this app only ever sees the resulting session.
 */

import { NextResponse } from "next/server";
import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

export const dynamic = "force-dynamic";

/**
 * Where to send the user afterwards.
 *
 * Only same-site paths are honoured. `next` arrives in a URL the user was sent
 * to, so treating it as trusted would turn sign-in into an open redirect: a
 * crafted link could bounce a freshly authenticated user to an attacker's page.
 */
function safeNext(raw: string | null): string {
  if (!raw) return "/dashboard";
  // Reject protocol-relative (//evil.com) and absolute URLs alike.
  if (!raw.startsWith("/") || raw.startsWith("//") || raw.includes("\\")) {
    return "/dashboard";
  }
  return raw;
}

export async function GET(request: Request) {
  const url = new URL(request.url);
  const code = url.searchParams.get("code");
  const next = safeNext(url.searchParams.get("next"));

  // Google reports a refusal by redirecting here with an error, not a code --
  // e.g. the user pressed Cancel on the consent screen.
  const oauthError = url.searchParams.get("error");
  if (oauthError) {
    const description =
      url.searchParams.get("error_description") ?? "Sign-in was cancelled.";
    return NextResponse.redirect(
      new URL(`/login?error=${encodeURIComponent(description)}`, url.origin),
    );
  }

  if (!code) {
    return NextResponse.redirect(
      new URL("/login?error=Missing+sign-in+code", url.origin),
    );
  }

  const supabaseUrl = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const supabaseKey = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!supabaseUrl || !supabaseKey) {
    return NextResponse.redirect(
      new URL("/login?error=Auth+is+not+configured", url.origin),
    );
  }

  // Redirect first, then hand the client this response to write cookies onto,
  // so the session is set on the very hop that lands in the dashboard.
  const response = NextResponse.redirect(new URL(next, url.origin));
  const cookieStore = await cookies();

  const supabase = createServerClient(supabaseUrl, supabaseKey, {
    cookies: {
      getAll: () => cookieStore.getAll(),
      setAll: (toSet) => {
        toSet.forEach(({ name, value, options }) =>
          response.cookies.set(name, value, options),
        );
      },
    },
  });

  const { error } = await supabase.auth.exchangeCodeForSession(code);
  if (error) {
    // A code is single-use and short-lived; a stale or replayed one lands here.
    console.error("OAuth code exchange failed:", error.message);
    return NextResponse.redirect(
      new URL("/login?error=Could+not+complete+sign-in", url.origin),
    );
  }

  return response;
}
