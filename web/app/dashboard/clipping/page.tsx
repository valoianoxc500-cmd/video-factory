import { createClient } from "@/lib/supabase/server";
import { AssetRepository, TaskRepository } from "@/lib/vrf";
import { ViralClipping } from "@/components/reels/ViralClipping";

export const dynamic = "force-dynamic";

/**
 * Clipping: upload a video you own, cut it, and download the result.
 *
 * The list passed down is filtered to assets that actually have a file. A
 * link-added video whose platform offers no downloadable media has nothing to
 * cut, and offering it here would be a control that fails when pressed — that
 * filter is why this screen used to be empty, and why uploading now lives on
 * the page rather than behind a trip to My Videos.
 */
export default async function ClippingPage() {
  const supabase = await createClient();

  let assets: Awaited<ReturnType<AssetRepository["listForUser"]>> = [];
  let latest = null;
  try {
    assets = await new AssetRepository(supabase).listForUser();
    const tasks = new TaskRepository(supabase);
    const [processTask, ingestTask] = await Promise.all([
      tasks.latest("process"), tasks.latest("ingest"),
    ]);
    latest = [processTask, ingestTask]
      .filter((item): item is NonNullable<typeof item> => Boolean(item?.payload?.auto_clip))
      .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0] ?? null;
  } catch {
    // The workspace still opens on its upload box and reports its own errors.
  }

  const clippable = assets.filter((asset) =>
    Boolean(String(asset.storage_path ?? "").trim()),
  );

  return (
    <>
      <div
        className="page-head"
        data-art="reels"
        data-surface="clipping"
        style={{ ["--head-art" as string]: "url('/channels/reels.jpg')" }}
      >
        <span className="engine-kicker">Clipping</span>
        <h1>
          AI <span className="hl">Clipping</span>
        </h1>
        <p>
          Turn any video into viral clips for TikTok, Shorts, and Reels.
        </p>
      </div>

      <ViralClipping
        initialAssets={clippable.map((a) => ({
          id: a.id,
          title: a.title,
          duration_seconds: a.duration_seconds,
          width: a.width,
          height: a.height,
          thumbnail_url: a.thumbnail_url,
          storage_path: a.storage_path,
          processed_path: a.processed_path,
          ingest_status: a.ingest_status,
          ingest_detail: a.ingest_detail,
        }))}
        initialTask={
          latest
            ? {
                id: latest.id,
                status: latest.status,
                error: latest.error,
                payload: latest.payload,
                result: latest.result,
                // Carried so the screen can tell a job that is working from
                // one that was never claimed.
                created_at: latest.created_at,
              }
            : null
        }
      />
    </>
  );
}
