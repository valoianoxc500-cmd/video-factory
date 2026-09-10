import { createClient } from "@/lib/supabase/server";
import { AssetRepository, TaskRepository } from "@/lib/vrf";
import { Clipping } from "@/components/reels/Clipping";

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
    latest = await new TaskRepository(supabase).latest("process");
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
          Cut a <span className="hl">clip</span>
        </h1>
        <p>
          Upload a video you own, choose the part worth watching, and get it
          back reframed, captioned and ready to post — without leaving this
          page.
        </p>
      </div>

      <Clipping
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
              }
            : null
        }
      />
    </>
  );
}
