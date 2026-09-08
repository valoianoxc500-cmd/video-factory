import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { AccountRepository, AssetRepository, PLATFORMS } from "@/lib/vrf";
import { MyVideos } from "@/components/reels/MyVideos";

export const dynamic = "force-dynamic";

export default async function MyVideosPage() {
  const supabase = await createClient();
  const [assets, connected] = await Promise.all([
    new AssetRepository(supabase).listForUser(),
    new AccountRepository(supabase).listForUser(),
  ]);

  const connectedPlatforms = new Set(connected.map((a) => a.platform));
  const accounts = PLATFORMS.map((platform) => ({
    platform: platform.platform,
    connected: connectedPlatforms.has(platform.platform),
  }));

  return (
    <>
      <div
        className="page-head"
        data-art="reels"
        style={{ ["--head-art" as string]: "url('/channels/reels.jpg')" }}
      >
        <h1>
          Re-<span className="hl">Create</span>
        </h1>
        <p>
          Videos you hold the rights to, and their new versions. Each one is
          re-encoded for the target platform, reframed to 9:16 around the
          subject and loudness-normalised — the format work an editor does to
          their own footage. To keep only part of a video,{" "}
          <Link href="/dashboard/clipping" className="inline-link">
            cut a clip
          </Link>
          .
        </p>
      </div>
      <MyVideos initial={assets} accounts={accounts} />
    </>
  );
}
