import { isWorkerAuthorized, workerClaimJob } from "@/lib/worker-db";

/**
 * The worker polls this to pick up the next queued job.
 *
 * Two secrets are checked: the bearer token here, and the same token again
 * inside the database function, which is what actually authorises the row
 * change. A caller who reaches this route without the token gets nothing.
 */

export const dynamic = "force-dynamic";
export const maxDuration = 30;

export async function POST(request: Request) {
  if (!isWorkerAuthorized(request)) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }
  try {
    const job = await workerClaimJob();
    return Response.json({ job: job ?? null, reason: job ? undefined : "queue empty" });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("worker claim failed:", message);
    return Response.json({ job: null, error: "Could not claim a job." }, { status: 503 });
  }
}
