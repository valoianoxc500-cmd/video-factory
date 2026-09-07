"use client";

import { Suspense, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { AuthShell } from "@/components/AuthShell";
import { Brand } from "@/components/Brand";
import { GoogleButton } from "@/components/GoogleButton";
import { createClient } from "@/lib/supabase/client";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reveal, setReveal] = useState(false);
  // Supabase already persists the session; this reflects that default rather
  // than promising a second behaviour the client does not have.
  const [remember, setRemember] = useState(true);

  const notice = params.get("reset") === "1" ? "Password updated. Sign in with your new password." : null;
  // The OAuth callback reports failures by bouncing back here with ?error=.
  const callbackError = params.get("error");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const { error: signInError } = await createClient().auth.signInWithPassword({
        email: email.trim(),
        password,
      });
      if (signInError) {
        // Deliberately not distinguishing "no such account" from "wrong
        // password": that difference tells an attacker which emails exist.
        setError("That email and password do not match an account.");
        return;
      }
      const next = params.get("next");
      router.push(next && next.startsWith("/") ? next : "/dashboard");
      router.refresh();
    } catch {
      setError("Could not reach the server. Try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Brand showTagline={false} badge="SaaS" />
      <h1>Welcome back</h1>
      <p className="auth-sub">Sign in to your account to continue.</p>

      {notice && <div className="notice notice-ok">{notice}</div>}
      {(error || callbackError) && (
        <div className="notice notice-error">{error ?? callbackError}</div>
      )}

      <form onSubmit={submit} noValidate>
        <div className="field field-icon">
          <label htmlFor="email">Email</label>
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path
              d="M4 6h16v12H4z M4 7l8 6 8-6"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <input
            id="email"
            type="email"
            autoComplete="email"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            placeholder="Email address"
          />
        </div>

        <div className="field field-icon">
          <label htmlFor="password">Password</label>
          <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path
              d="M7 10V8a5 5 0 0 1 10 0v2M6 10h12v10H6z"
              stroke="currentColor"
              strokeWidth="1.7"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
          <input
            id="password"
            type={reveal ? "text" : "password"}
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="Password"
          />
          <button
            type="button"
            className="field-reveal"
            onClick={() => setReveal((v) => !v)}
            aria-label={reveal ? "Hide password" : "Show password"}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none">
              <path
                d={
                  reveal
                    ? "M3 3l18 18M10.6 10.6a2 2 0 0 0 2.8 2.8M9.9 5.2A9.8 9.8 0 0 1 12 5c5 0 9 4.5 9 7a12 12 0 0 1-2.4 3.4M6.5 6.6C4.2 8.1 3 10.4 3 12c0 2.5 4 7 9 7a9.6 9.6 0 0 0 3.6-.7"
                    : "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Zm10 2.6a2.6 2.6 0 1 0 0-5.2 2.6 2.6 0 0 0 0 5.2Z"
                }
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>

        <div className="auth-row">
          <label className="auth-remember">
            <input
              type="checkbox"
              checked={remember}
              onChange={(e) => setRemember(e.target.checked)}
            />
            Remember me
          </label>
          <Link href="/forgot-password">Forgot password?</Link>
        </div>

        <button className="btn-primary" type="submit" disabled={busy}>
          {busy ? "Signing in…" : "Sign in  →"}
        </button>
      </form>

      <div className="auth-divider">
        <span>or</span>
      </div>

      <GoogleButton next={params.get("next")} />

      <p className="auth-alt">
        Don&apos;t have an account? <Link href="/signup">Sign up</Link>
      </p>
    </>
  );
}

export default function LoginPage() {
  return (
    <AuthShell>
      <Suspense fallback={<div className="skeleton" style={{ height: 300 }} />}>
        <LoginForm />
      </Suspense>
    </AuthShell>
  );
}
