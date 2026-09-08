import { createClient, requireUser } from "@/lib/supabase/server";
import { AssetRepository, TaskRepository, toReelsHttpError } from "@/lib/vrf";

/**
 * Clipping: cut a section out of a video the user owns and reframe it to 9:16.
 *
 * This adds no new capability. `viral/processing.py` has always been able to
 * trim, reframe and re-encode; the worker's process task simply never had a
 * caller that asked for a trim, so every clip was the whole video. This route
 * is that caller.
 *
 * Only assets with a real file can be clipped. A metadata-only asset -- one
 * whose platform gives no official route to the media -- has nothing to cut,
 * and saying so here is kinder than queueing work that cannot start.
 */

export const dynamic = "force-dynamic";

/** Long enough to be worth cutting down; short enough to be a clip. */
const MIN_CLIP_SECONDS = 1;

export async function GET() {
  try {
    await requireUser();
    const supabase = await createClient();
    const assets = await new AssetRepository(supabase).listForUser();

    // Only what can actually be clipped: something with a file on disk.
    const clippable = assets.filter((asset) =>
      Boolean(String(asset.storage_path ?? "").trim()),
    );

    let running = null;
    try {
      running = await new TaskRepository(supabase).latest("process");
    } catch {
      // The list is still useful without the task's state.
    }

    return Response.json({ assets: clippable, latest: running });
  } catch (err) {
    const { status, message } = toReelsHttpError(err);
    return Response.json({ error: message, assets: [], latest: null }, { status });
  }
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return Response.json({ error: "Invalid request body." }, { status: 400 });
    }
    const payload = (body ?? {}) as Record<string, unknown>;

    const assetId = String(payload.assetId ?? "").trim();
    if (!assetId) {
      return Response.json({ error: "Choose a video to clip." }, { status: 400 });
    }

    const supabase = await createClient();

    // Read the asset back rather than trusting the request: whether it has a
    // file, and how long it runs, decide whether the trim is even possible.
    const assets = await new AssetRepository(supabase).listForUser();
    const asset = assets.find((row) => row.id === assetId);
    if (!asset) {
      return Response.json({ error: "That video no longer exists." }, { status: 404 });
    }
    if (!String(asset.storage_path ?? "").trim()) {
      return Response.json(
        {
          error:
            asset.ingest_detail ||
            "That video has no imported file yet, so there is nothing to clip.",
        },
        { status: 409 },
      );
    }

    const trimStart = Math.max(0, Number(payload.trimStart) || 0);
    const trimEnd = Math.max(0, Number(payload.trimEnd) || 0);

    // A trim that removes everything is a mistake worth catching before it
    // reaches ffmpeg, which would fail with something far less helpful.
    const duration = Number(asset.duration_seconds ?? 0);
    if (duration > 0 && duration - trimStart - trimEnd < MIN_CLIP_SECONDS) {
      return Response.json(
        {
          error: `Those trims leave nothing behind. The video is ${Math.round(
            duration,
          )}s long.`,
        },
        { status: 400 },
      );
    }

    const task = await new TaskRepository(supabase).create(user.id, "process", {
      asset_id: asset.id,
      platform: String(payload.platform ?? "tiktok"),
      trim_start: trimStart,
      trim_end: trimEnd,
    });

    return Response.json({ task }, { status: 201 });
  } catch (err) {
    const { status, message } = toReelsHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
