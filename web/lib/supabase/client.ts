"use client";

import { createBrowserClient } from "@supabase/ssr";

/**
 * Browser Supabase client.
 *
 * Carries only the publishable key, which is designed to be public: every
 * table is protected by RLS, so this key alone grants nothing. It exists for
 * sign-in, sign-out and password reset -- data is fetched through the app's
 * own API routes, which authorize server-side.
 */
export function createClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
  if (!url || !key) {
    throw new Error("Auth is not configured for this deployment.");
  }
  return createBrowserClient(url, key);
}
