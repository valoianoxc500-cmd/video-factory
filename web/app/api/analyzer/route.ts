import { createClient, requireUser } from "@/lib/supabase/server";
import { AssetRepository, TaskRepository, toReelsHttpError } from "@/lib/vrf";

/**
 * Clip Analyzer: read a video and explain why it performed.
 *
 * Queues an `explain` task and reports on it. The analysis itself runs on the
 * worker, because looking at a video means sampling frames with ffmpeg and
 * calling a vision model, and neither of those belongs in a request handler.
 *
 * This route can start an analysis and read one. It cannot start a
 * generation: the analyser explains and stops, and the plan it returns is for
 * a person to carry out.
 */

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  try {
    await requireUser();
    const supabase = await createClient();
    const taskId = new URL(request.url).searchParams.get("task");

    // Polling one running analysis.
    if (taskId) {
      const task = await new TaskRepository(supabase).get(taskId);
      return Response.json({ task });
    }

    const assets = await new AssetRepository(supabase).listForUser();

    let latest = null;
    try {
      latest = await new TaskRepository(supabase).latest("explain");
    } catch {
      // The list is still useful without the last analysis.
    }

    return Response.json({ assets, latest });
  } catch (err) {
    const { status, message } = toReelsHttpError(err);
    return Response.json({ error: message, assets: [], latest: null }, { status });
  }
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();
    const supabase = await createClient();

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return Response.json({ error: "Invalid request body." }, { status: 400 });
    }
    const assetId = String(
      (body as Record<string, unknown> | null)?.assetId ?? "",
    ).trim();
    if (!assetId) {
      return Response.json({ error: "Choose a video to analyse." }, { status: 400 });
    }

    // Reading it first is the authorisation check: RLS scopes the repository
    // to this user, so an asset belonging to someone else is simply not found.
    const asset = await new AssetRepository(supabase).get(assetId);

    const tasks = new TaskRepository(supabase);
    if ((await tasks.activeCount("explain")) > 0) {
      return Response.json(
        { error: "An analysis is already running." },
        { status: 409 },
      );
    }

    const task = await tasks.create(user.id, "explain", {
      asset_id: asset.id,
    });
    return Response.json({ task }, { status: 201 });
  } catch (err) {
    const { status, message } = toReelsHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
