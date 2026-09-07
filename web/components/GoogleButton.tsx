"use client";

import { useState } from "react";
import { createClient } from "@/lib/supabase/client";

/**
 * "Continue with Google".
 *
 * The redirect target is built from window.location.origin rather than a
 * configured site URL, so the same build works on localhost, on every Vercel
 * preview URL, and in production without an env var per environment. Supabase
 * still has to allow-list those origins -- see DEPLOYMENT.md.
 *
 * No Google credential is referenced here. The browser is sent to Supabase,
 * which holds the client secret and performs the exchange.
 */
export function GoogleButton({
  label = "Continue with Google",
  next,
}: {
  label?: string;
  next?: string | null;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function start() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const target = new URL("/auth/callback", window.location.origin);
      if (next && next.startsWith("/") && !next.startsWith("//")) {
        target.searchParams.set("next", next);
      }

      const { error: oauthError } = await createClient().auth.signInWithOAuth({
        provider: "google",
        options: {
          redirectTo: target.toString(),
          // No custom queryParams. `prompt=select_account` used to be sent here
          // to force the account chooser; the first sign-in (fresh consent)
          // succeeded with it, but every later attempt -- where consent already
          // existed -- failed inside Google after account selection and never
          // redirected back. Google validates the parameter fine, so this is
          // the plain documented flow rather than a workaround.
        },
      });
      if (oauthError) {
        setError(
          oauthError.message.toLowerCase().includes("provider")
            ? "Google sign-in is not enabled for this project yet."
            : "Could not start Google sign-in.",
        );
        setBusy(false);
      }
      // On success the browser navigates away; leave the button disabled.
    } catch {
      setError("Could not reach the server.");
      setBusy(false);
    }
  }

  return (
    <>
      {error && <div className="notice notice-error">{error}</div>}
      <button
        type="button"
        className="btn-google"
        onClick={() => void start()}
        disabled={busy}
      >
        <svg width="17" height="17" viewBox="0 0 18 18" aria-hidden focusable="false">
          <path
            fill="#4285F4"
            d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62Z"
          />
          <path
            fill="#34A853"
            d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.81.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18Z"
          />
          <path
            fill="#FBBC05"
            d="M3.97 10.72a5.4 5.4 0 0 1 0-3.44V4.95H.96a9 9 0 0 0 0 8.1l3.01-2.33Z"
          />
          <path
            fill="#EA4335"
            d="M9 3.58c1.32 0 2.5.45 3.44 1.35l2.58-2.58C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58Z"
          />
        </svg>
        {busy ? "Redirecting…" : label}
      </button>
    </>
  );
}
