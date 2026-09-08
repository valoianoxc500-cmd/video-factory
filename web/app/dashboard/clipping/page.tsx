import { createClient } from "@/lib/supabase/server";
import { AssetRepository, TaskRepository } from "@/lib/vrf";
import { Clipping } from "@/components/reels/Clipping";

export const dynamic = "force-dynamic";

/**
 * Clipping: a long video you own, cut down and reframed to 9:16.
 *
 * The list is filtered to assets that actually have an imported file, because
 * those are the only ones the worker can cut. Everything else would be a
 * control that fails when pressed.
 */

export default async function ClippingPage() {
  const supabase = await createClient();

  let assets: Awaited<ReturnType<AssetRepository["listForUser"]>> = [];
  let latest = null;
  try {
    assets = await new AssetRepository(supabase).listForUser();
    latest = await new TaskRepository(supabase).latest("process");
  } catch {
    // The panel renders empty and reports its own failures.
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
        <h1>
          Cut a <span className="hl">clip</span>
        </h1>
        <p>
          Take a video you own, trim it to the part worth watching, and let it
          be reframed to 9:16 around the subject. Nothing is invented and
          nothing is added — this is the same trim, crop and re-encode the
          worker already runs, with the section to keep chosen by you.
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
          source_platform: a.source_platform,
          source_author: a.source_author,
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
