import { isWorkerAuthorized } from "@/lib/worker-db";
import { aiVideoUpdateJob } from "@/lib/aivideo-db";

/**
 * Progress and completion for one AI Video Maker job.
 *
 * The worker calls this at every stage boundary and once at the end. Only the
 * fields it sends are written, so a progress ping cannot blank a result that
 * a later call already recorded.
 */

export const dynamic = "force-dynamic";
export const maxDuration = 30;

export async function POST(request: Request) {
  if (!isWorkerAuthorized(request)) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }
  try {
    const body = await request.json();
    const id = String(body?.id ?? "");
    if (!id) {
      return Response.json({ error: "Missing job id" }, { status: 400 });
    }
    await aiVideoUpdateJob(id, body);
    return Response.json({ ok: true });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("aivideo update failed:", message);
    return Response.json({ error: "Could not update the job." }, { status: 503 });
  }
}
