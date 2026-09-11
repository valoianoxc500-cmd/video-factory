import { createClient, requireUser } from "@/lib/supabase/server";
import { AssetRepository, TaskRepository, toReelsHttpError } from "@/lib/vrf";
import { mediaImportPlan, parseVideoUrl } from "@/lib/vrf-ingest";

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

const LANGUAGES = new Set(["auto", "en", "ar", "es"]);
const LENGTHS = new Set(["auto", "short", "medium", "long"]);
const COUNTS = new Set(["auto", "3", "5", "10"]);

function option(value: unknown, allowed: Set<string>, fallback: string): string {
  const clean = String(value ?? "").trim().toLowerCase();
  return allowed.has(clean) ? clean : fallback;
}

function publicTask(task: Awaited<ReturnType<TaskRepository["get"]>> | null) {
  if (!task) return null;
  const clips = (Array.isArray(task.result?.clips) ? task.result.clips : []).map((item) => {
    const row = (item ?? {}) as Record<string, unknown>;
    const { path: _path, error: _error, ...safe } = row;
    return safe;
  });
  return { ...task, result: { ...task.result, clips } };
}

export async function GET(request: Request) {
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
      const tasks = new TaskRepository(supabase);
      const requested = new URL(request.url).searchParams.get("task");
      if (requested) {
        running = await tasks.get(requested);
      } else {
        const [processTask, ingestTask] = await Promise.all([
          tasks.latest("process"), tasks.latest("ingest"),
        ]);
        running = [processTask, ingestTask]
          .filter((item): item is NonNullable<typeof item> => Boolean(item?.payload?.auto_clip))
          .sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0] ?? null;
      }
    } catch {
      // The list is still useful without the task's state.
    }

    return Response.json({ assets: clippable, latest: publicTask(running) });
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

    if (payload.ownsOrPermitted !== true) {
      return Response.json(
        { error: "Confirm you own this video or have permission to use it." },
        { status: 400 },
      );
    }

    const raw = (payload.clipOptions ?? {}) as Record<string, unknown>;
    const autoClip = {
      language: option(raw.language, LANGUAGES, "auto"),
      length: option(raw.length, LENGTHS, "auto"),
      count: option(raw.count, COUNTS, "auto"),
    };
    const supabase = await createClient();
    const assets = new AssetRepository(supabase);
    const tasks = new TaskRepository(supabase);
    const sourceUrl = String(payload.sourceUrl ?? "").trim();

    if (sourceUrl) {
      const parsed = parseVideoUrl(sourceUrl);
      const { data, error } = await supabase
        .from("vrf_accounts")
        .select("platform, revoked_at")
        .is("revoked_at", null);
      if (error) throw new Error(error.message);
      const connected = (data ?? []).map((row) => String(row.platform));
      const plan = mediaImportPlan(parsed, connected);
      if (!plan.canFetchMedia) {
        return Response.json({ error: plan.reason }, { status: 409 });
      }
      const asset = await assets.createFromUrl(user.id, {
        url: sourceUrl,
        ownsOrPermitted: true,
        connectedPlatforms: connected,
      });
      const task = await tasks.create(user.id, "ingest", {
        asset_id: asset.id,
        platform: "tiktok",
        auto_clip: autoClip,
      });
      return Response.json({ task: publicTask(task) }, { status: 201 });
    }

    const assetId = String(payload.assetId ?? "").trim();
    if (!assetId) {
      return Response.json(
        { error: "Paste a supported link or upload a video." },
        { status: 400 },
      );
    }
    const asset = await assets.get(assetId);
    if (!String(asset.storage_path ?? "").trim()) {
      return Response.json(
        { error: "This video does not include a processable source file. Upload the original video to create clips." },
        { status: 409 },
      );
    }
    const task = await tasks.create(user.id, "process", {
      asset_id: asset.id,
      platform: "tiktok",
      auto_clip: autoClip,
    });
    return Response.json({ task: publicTask(task) }, { status: 201 });
  } catch (err) {
    const { status, message } = toReelsHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
