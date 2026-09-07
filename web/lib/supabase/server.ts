import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

/**
 * Supabase client bound to the caller's session cookies.
 *
 * Only the publishable key is used, never a service-role key: the browser must
 * never receive one, and the server does not need one because every table is
 * scoped by RLS to auth.uid(). A query that forgets a filter returns the
 * caller's rows or nothing -- it cannot return somebody else's.
 */
export async function createClient() {
  const cookieStore = await cookies();
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) {
    throw new Error(
      "Auth is not configured: NEXT_PUBLIC_SUPABASE_URL and " +
        "NEXT_PUBLIC_SUPABASE_ANON_KEY must be set.",
    );
  }

  return createServerClient(url, key, {
    cookies: {
      getAll: () => cookieStore.getAll(),
      setAll: (toSet) => {
        try {
          toSet.forEach(({ name, value, options }) =>
            cookieStore.set(name, value, options),
          );
        } catch {
          // Called from a Server Component, where cookies are read-only.
          // Refreshing happens in middleware, so this is safe to ignore.
        }
      },
    },
  });
}

/**
 * The signed-in user, or null.
 *
 * Uses getUser(), which revalidates the token with Supabase, rather than
 * getSession(), which trusts whatever cookie the browser sent. Authorization
 * must never rest on a value the client could have written.
 */
export async function getUser() {
  const supabase = await createClient();
  const { data, error } = await supabase.auth.getUser();
  if (error) return null;
  return data.user ?? null;
}

/** The signed-in user, or throw. For routes that must not run anonymously. */
export async function requireUser() {
  const user = await getUser();
  if (!user) {
    throw new UnauthorizedError();
  }
  return user;
}

export class UnauthorizedError extends Error {
  readonly status = 401;
  constructor() {
    super("You must be signed in to do that.");
    this.name = "UnauthorizedError";
  }
}
