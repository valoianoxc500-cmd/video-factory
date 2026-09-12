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
          "cost_usd, created_at",
      )
      .eq("user_id", user.id)
      .order("created_at", { ascending: false })
      .limit(24);
    if (error) throw new Error(error.message);
    return Response.json({ jobs: data ?? [] });
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
