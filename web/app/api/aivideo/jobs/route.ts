import { createClient, requireUser } from "@/lib/supabase/server";
import { sanitiseSettings } from "@/lib/aivideo";

/**
 * AI Video Maker jobs: list the customer's own, and queue a new one.
 *
 * Its own table and its own worker queue, so a stuck generation here cannot
 * hold up Quote Studio or the reels worker. The customer's saved defaults are
 * written on every create — returning tomorrow should restore the setup they
 * were last happy with, without them having to press anything.
 */

export const dynamic = "force-dynamic";

/**
 * How long a job may sit unclaimed, or claimed but silent, before the customer
 * is told something is wrong.
 *
 * A worker that is down produces no error anywhere: the row simply stays
 * `queued` and the UI says "Waiting to start — 0%" indefinitely, which is
 * exactly how this product looked before the worker was installed. Nothing in
 * the pipeline can report that, because the pipeline never ran. So the read
 * path decides it, from the row's own timestamps.
 *
 * Generous on purpose. A queued job normally waits seconds; a running one
 * reports progress every stage boundary, and the slowest stage observed is the
 * render at roughly four minutes.
 */
const UNCLAIMED_STALL_MS = 6 * 60 * 1000;
const SILENT_STALL_MS = 12 * 60 * 1000;

type JobRow = Record<string, unknown> & {
  status?: string;
  created_at?: string;
  updated_at?: string;
};

/**
 * Annotate a job the worker has plainly not touched.
 *
 * Deliberately a *view*, not a write: this does not change the row, so the
 * job stays exactly where it is and a worker coming back later still picks it
 * up and finishes it. Nothing here can create a duplicate job, and a stalled
 * job that later completes simply stops being reported as stalled.
 */
function withStallState<T extends JobRow>(job: T): T & { stalled?: boolean } {
  if (job.status !== "queued" && job.status !== "running") return job;

  const since = Date.parse(
    (job.status === "queued" ? job.created_at : job.updated_at) ?? "",
  );
  if (!Number.isFinite(since)) return job;

  const limit = job.status === "queued" ? UNCLAIMED_STALL_MS : SILENT_STALL_MS;
  if (Date.now() - since < limit) return job;

  return {
    ...job,
    stalled: true,
    message:
      "This is taking longer than usual. It's still queued and will start " +
      "as soon as capacity frees up — you don't need to submit it again.",
  };
}

function fail(err: unknown, action: string) {
  const message = (err as Error)?.message ?? "";
  if (/sign|auth|session|jwt/i.test(message)) {
    return Response.json({ error: "You must be signed in." }, { status: 401 });
  }
  console.error(`aivideo ${action} failed:`, message);
  return Response.json({ error: `Could not ${action}.` }, { status: 500 });
}

export async function GET() {
  try {
    const user = await requireUser();
    const db = await createClient();
    const { data, error } = await db
      .from("ai_video_jobs")
      .select(
        "id, topic, language, duration_seconds, aspect_ratio, status, stage, " +
          "progress, message, error, video_url, thumbnail_url, duration_actual, " +
          "cost_usd, created_at, updated_at, attempts, claimed_at",
      )
      .eq("user_id", user.id)
      .order("created_at", { ascending: false })
      .limit(24);
    if (error) throw new Error(error.message);
    // Cast because the client widens a long select into a union that includes
    // its own error shape; `error` above is what actually distinguishes them.
    const rows = (data ?? []) as unknown as JobRow[];
    return Response.json({ jobs: rows.map(withStallState) });
  } catch (err) {
    return fail(err, "load your videos");
  }
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();
    const body = await request.json().catch(() => ({}));

    const topic = String(body?.topic ?? "").trim();
    if (topic.length < 3) {
      return Response.json(
        { error: "Tell us what the video should be about." },
        { status: 400 },
      );
    }
    if (topic.length > 400) {
      return Response.json(
        { error: "That topic is a little long — try a shorter idea." },
        { status: 400 },
      );
    }

    const settings = sanitiseSettings(body?.settings);
    const db = await createClient();

    // One running job per customer. Two concurrent renders on a single worker
    // just make both slower, and the queue is the honest place to say so.
    const { count } = await db
      .from("ai_video_jobs")
      .select("id", { count: "exact", head: true })
      .eq("user_id", user.id)
      .in("status", ["queued", "running"]);
    if ((count ?? 0) >= 2) {
      return Response.json(
        { error: "You already have a video generating. It'll be ready shortly." },
        { status: 429 },
      );
    }

    const { data, error } = await db
      .from("ai_video_jobs")
      .insert({
        user_id: user.id,
        topic,
        language: settings.language,
        duration_seconds: settings.duration_seconds,
        aspect_ratio: settings.aspect_ratio,
        spec: { topic, ...settings },
        status: "queued",
        message: "Preparing your video",
      })
      .select("id")
      .single();
    if (error) throw new Error(error.message);

    // Remember the setup. Best effort: a preferences write must never be the
    // reason a customer's video did not get queued.
    void db
      .from("ai_video_prefs")
      .upsert(
        { user_id: user.id, settings, updated_at: new Date().toISOString() },
        { onConflict: "user_id" },
      )
      .then(({ error: prefErr }) => {
        if (prefErr) console.warn("aivideo prefs autosave failed:", prefErr.message);
      });

    return Response.json({ id: data.id });
  } catch (err) {
    return fail(err, "start that video");
  }
}
