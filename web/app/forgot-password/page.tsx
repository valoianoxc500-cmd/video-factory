"use client";

import { useState } from "react";
import Link from "next/link";
import { AuthShell } from "@/components/AuthShell";
import { createClient } from "@/lib/supabase/client";

export default function ForgotPasswordPage() {
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    try {
      await createClient().auth.resetPasswordForEmail(email.trim(), {
        redirectTo: `${window.location.origin}/reset-password`,
      });
    } catch {
      /* fall through: the confirmation below is deliberately unconditional */
    } finally {
      // Always the same answer, whether or not that address has an account:
      // a different response would confirm which emails are registered.
      setSent(true);
      setBusy(false);
    }
  }

  return (
    <AuthShell>
      <h1>Reset your password</h1>
      <p className="auth-sub">We will email you a link to choose a new one.</p>

      {sent ? (
        <div className="notice notice-ok">
          If an account exists for that address, a reset link is on its way.
          The link expires in one hour.
        </div>
      ) : (
        <form onSubmit={submit} noValidate>
          <div className="field">
            <label htmlFor="email">Email</label>
            <input
              id="email"
              type="email"
              autoComplete="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="you@example.com"
            />
          </div>
          <button className="btn-primary" type="submit" disabled={busy}>
            {busy ? "Sending…" : "Send reset link"}
          </button>
        </form>
      )}

      <p className="auth-alt">
        <Link href="/login">Back to sign in</Link>
      </p>
    </AuthShell>
  );
}
