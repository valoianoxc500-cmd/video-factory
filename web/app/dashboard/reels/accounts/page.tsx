import { createClient } from "@/lib/supabase/server";
import { AccountRepository, PLATFORMS } from "@/lib/vrf";
import { OAUTH_PROVIDERS } from "@/lib/vrf-oauth";
import { tokenKeyConfigured } from "@/lib/vrf-crypto";
import { Accounts } from "@/components/reels/Accounts";

export const dynamic = "force-dynamic";

export default async function AccountsPage({
  searchParams,
}: {
  searchParams: Promise<{ connected?: string; error?: string }>;
}) {
  const flash = await searchParams;
  const supabase = await createClient();
  const connected = await new AccountRepository(supabase).listForUser();
  const byPlatform = new Map(connected.map((a) => [a.platform, a]));

  const platforms = PLATFORMS.map((platform) => {
    const provider = OAUTH_PROVIDERS[platform.platform];
    return {
      ...platform,
      configured: Boolean(
        provider &&
          provider.flow !== "unsupported" &&
          process.env[provider.clientIdEnv] &&
          process.env[provider.clientSecretEnv],
      ),
      connected: byPlatform.has(platform.platform),
      account: byPlatform.get(platform.platform) ?? null,
      scopes: provider?.scopes ?? [],
    };
  });

  return (
    <>
      <div
        className="page-head"
        data-art="reels"
        style={{ ["--head-art" as string]: "url('/channels/reels.jpg')" }}
      >
        <h1>Connected Accounts</h1>
        <p>
          Publishing happens through each platform&rsquo;s official API, using
          permissions you grant on their own consent screen.
        </p>
      </div>
      <Accounts
        platforms={platforms}
        encryptionReady={tokenKeyConfigured()}
        flash={flash}
      />
    </>
  );
}
