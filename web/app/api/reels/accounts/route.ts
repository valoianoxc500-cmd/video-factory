import { createClient, requireUser } from "@/lib/supabase/server";
import { toHttpError } from "@/lib/repositories";
import { AccountRepository, CONNECTABLE_PLATFORMS } from "@/lib/vrf";
import { OAUTH_PROVIDERS } from "@/lib/vrf-oauth";
import { tokenKeyConfigured } from "@/lib/vrf-crypto";

/**
 * Connected accounts, per platform.
 *
 * The response carries handles and expiry dates. It never carries token
 * material, encrypted or otherwise: the repository does not select those
 * columns at all.
 */

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    await requireUser();
    const supabase = await createClient();
    const connected = await new AccountRepository(supabase).listForUser();
    const byPlatform = new Map(connected.map((a) => [a.platform, a]));

    // CONNECTABLE_PLATFORMS, not PLATFORMS: Threads and X are offered here so
    // they can be connected for Quote Studio, while staying out of the video
    // publishing list the Reels queue iterates.
    const platforms = CONNECTABLE_PLATFORMS.map((platform) => {
      const provider = OAUTH_PROVIDERS[platform.platform];
      const account = byPlatform.get(platform.platform) ?? null;
      return {
        ...platform,
        configured: Boolean(
          provider &&
            provider.flow !== "unsupported" &&
            process.env[provider.clientIdEnv] &&
            process.env[provider.clientSecretEnv],
        ),
        connected: account !== null,
        account,
        scopes: provider?.scopes ?? [],
      };
    });

    return Response.json({
      platforms,
      // Without a key the connect flow refuses rather than storing a token in
      // the clear, so the UI needs to know before offering the button.
      encryptionReady: tokenKeyConfigured(),
    });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message, platforms: [] }, { status });
  }
}
