"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import type { AccountRow, PlatformCapability } from "@/lib/vrf";

/**
 * Connect and disconnect social accounts.
 *
 * Connecting opens the platform's own consent screen. There is no password
 * field on this page, and there is no code path in this product that would
 * accept one.
 */

interface PlatformState extends PlatformCapability {
  configured: boolean;
  connected: boolean;
  account: AccountRow | null;
  scopes: string[];
}

export function Accounts({
  platforms,
  encryptionReady,
  flash,
}: {
  platforms: PlatformState[];
  encryptionReady: boolean;
  flash: { connected?: string; error?: string };
}) {
  const router = useRouter();
  const [error, setError] = useState(flash.error ?? "");

  async function disconnect(id: string) {
    const response = await fetch(`/api/reels/accounts/${id}`, { method: "DELETE" });
    if (!response.ok) {
      setError("Could not disconnect that account.");
      return;
    }
    router.refresh();
  }

  return (
    <>
      {flash.connected && (
        <p className="notice notice-ok">
          Connected {flash.connected}. You can publish to it now.
        </p>
      )}
      {error && <p className="notice notice-error">{error}</p>}
      {!encryptionReady && (
        <p className="notice notice-error">
          Token encryption is not configured on this deployment (VRF_TOKEN_KEY).
          Accounts cannot be connected until it is — tokens are never stored
          unencrypted.
        </p>
      )}

      <div className="reels-accounts">
        {platforms.map((platform) => (
          <article className="card" key={platform.platform}>
            <div className="reels-account-head">
              <h3>{platform.label}</h3>
              {platform.connected ? (
                <span className="chip chip-ok">Connected</span>
              ) : platform.supported ? (
                <span className="chip">Not connected</span>
              ) : (
                <span className="chip chip-err">Not supported</span>
              )}
            </div>

            {platform.connected && platform.account && (
              <p className="vmeta">
                {platform.account.account_handle || platform.account.account_ref}
                {platform.account.token_expires_at
                  ? ` · token renews ${new Date(
                      platform.account.token_expires_at,
                    ).toLocaleDateString()}`
                  : ""}
              </p>
            )}

            {platform.note && <p className="note">{platform.note}</p>}
            {platform.supported && !platform.configured && (
              <p className="note">
                Not configured on this deployment yet. Needs {platform.requires}.
              </p>
            )}

            {platform.supported && (
              <p className="note">
                Permissions requested: {platform.scopes.join(", ") || "—"}
              </p>
            )}

            <div className="actions">
              {platform.connected && platform.account ? (
                <button
                  className="btn-ghost"
                  type="button"
                  onClick={() => disconnect(platform.account!.id)}
                >
                  Disconnect
                </button>
              ) : (
                <a
                  className={
                    platform.supported && platform.configured && encryptionReady
                      ? "btn-primary"
                      : "btn-ghost"
                  }
                  href={
                    platform.supported && platform.configured && encryptionReady
                      ? `/api/reels/accounts/connect?platform=${platform.platform}`
                      : undefined
                  }
                  aria-disabled={
                    !platform.supported || !platform.configured || !encryptionReady
                  }
                >
                  Connect
                </a>
              )}
            </div>
          </article>
        ))}
      </div>

      <p className="note">
        You are sent to each platform&rsquo;s own sign-in page. This application
        never asks for, receives or stores a social account password, and the
        tokens it receives are encrypted before they reach the database.
      </p>
    </>
  );
}
