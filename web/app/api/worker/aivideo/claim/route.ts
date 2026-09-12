import { isWorkerAuthorized } from "@/lib/worker-db";
import { aiVideoClaimJob } from "@/lib/aivideo-db";

/**
 * The AI Video Maker worker claims its next job here.
 *
 * A separate endpoint from `/api/worker/claim` on purpose: that one serves the
 * channel `jobs` queue, and the two products must not be able to starve or
 * block each other. Two secrets are checked, the bearer token here and the
 * same token again inside the database function, which is what actually
 * authorises the row change.
 */

export const dynamic = "force-dynamic";
export const maxDuration = 30;

export async function POST(request: Request) {
  if (!isWorkerAuthorized(request)) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }
  try {
    const job = await aiVideoClaimJob();
    return Response.json({ job: job ?? null });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("aivideo claim failed:", message);
    return Response.json({ job: null, error: "Could not claim a job." }, { status: 503 });
  }
}
