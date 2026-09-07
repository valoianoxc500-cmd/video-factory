import { createServerClient } from "@supabase/ssr";
import { NextResponse, type NextRequest } from "next/server";

/**
 * Session refresh and route protection.
 *
 * Runs on every matched request so the auth cookie is refreshed before a
 * Server Component reads it -- without this, sessions expire mid-visit and the
 * user is bounced to sign-in while still holding a valid refresh token.
 *
 * It also gates /dashboard. That is defence in depth, not the security
 * boundary: the real boundary is RLS plus per-route authorization, so a
 * middleware bug cannot expose another user's data.
 */

const PUBLIC_PATHS = [
  "/",
  "/login",
  "/signup",
  "/forgot-password",
  "/reset-password",
  "/auth/callback",
  // Legal pages must be readable by anyone, including Google's OAuth reviewers,
  // who fetch them without a session.
  "/privacy-policy",
  "/terms",
];

function isPublic(pathname: string): boolean {
  if (PUBLIC_PATHS.includes(pathname)) return true;
  // The worker authenticates with its own shared secret, not a user session.
  return pathname.startsWith("/api/worker");
}

/**
 * Supabase discards `redirect_to` when the URL is not in the project's
 * allow-list and falls back to the configured Site URL -- which drops the
 * /auth/callback path, landing the browser on `/?code=...` where nothing
 * handles the code and sign-in silently dies on the marketing page.
 *
 * Forwarding it to the callback recovers the flow whenever the landing origin
 * is the one sign-in started from. It cannot rescue a fallback to a *different*
 * origin: the PKCE verifier cookie lives on the origin that began the flow, so
 * the exchange there will fail by design. That case needs the origin
 * allow-listed in Supabase; see DEPLOYMENT.md section 8.1.
 */
function strayAuthCode(request: NextRequest): URL | null {
  const { pathname, searchParams } = request.nextUrl;
  if (pathname !== "/") return null;
  if (!searchParams.has("code") && !searchParams.has("error")) return null;

  const target = request.nextUrl.clone();
  target.pathname = "/auth/callback";
  if (!target.searchParams.has("next")) {
    target.searchParams.set("next", "/dashboard");
  }
  return target;
}

export async function middleware(request: NextRequest) {
  const stray = strayAuthCode(request);
  if (stray) return NextResponse.redirect(stray);

  let response = NextResponse.next({ request });

  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) return response;

  const supabase = createServerClient(url, key, {
    cookies: {
      getAll: () => request.cookies.getAll(),
      setAll: (toSet) => {
        toSet.forEach(({ name, value }) => request.cookies.set(name, value));
        response = NextResponse.next({ request });
        toSet.forEach(({ name, value, options }) =>
          response.cookies.set(name, value, options),
        );
      },
    },
  });

  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { pathname } = request.nextUrl;

  if (!user && !isPublic(pathname) && !pathname.startsWith("/api/")) {
    const redirect = request.nextUrl.clone();
    redirect.pathname = "/login";
    redirect.searchParams.set("next", pathname);
    return NextResponse.redirect(redirect);
  }

  // A signed-in user landing on an auth page belongs in the dashboard.
  if (user && ["/login", "/signup"].includes(pathname)) {
    const redirect = request.nextUrl.clone();
    redirect.pathname = "/dashboard";
    redirect.search = "";
    return NextResponse.redirect(redirect);
  }

  return response;
}

export const config = {
  matcher: [
    // Everything except Next internals and static files.
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
