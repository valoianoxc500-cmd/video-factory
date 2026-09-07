import { createClient, requireUser } from "@/lib/supabase/server";
import { toHttpError } from "@/lib/repositories";
import { TaskRepository } from "@/lib/vrf";
import { rateLimit } from "@/lib/rate-limit";

/**
 * Trending-video search.
 *
 * Discovery calls the YouTube Data API and costs quota, so the browser
 * enqueues a task and the Python worker runs it -- the same split the video
 * pipeline uses. GET returns the caller's most recent search; RLS means that
 * is their own search and nobody else's.
 */

export const dynamic = "force-dynamic";
export const maxDuration = 30;

export async function GET() {
  try {
    await requireUser();
    const supabase = await createClient();
    const task = await new TaskRepository(supabase).latest("discover");
    return Response.json({ task });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message, task: null }, { status });
  }
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();

    // Quota on the YouTube API is per-project, so one user's searching
    // otherwise spends everyone's budget.
    const limited = rateLimit(`reels:discover:${user.id}`, {
      limit: 30,
      windowSeconds: 60 * 10,
    });
    if (!limited.allowed) {
      return Response.json(
        { error: "Too many searches. Try again shortly." },
        {
          status: 429,
          headers: { "retry-after": String(limited.retryAfterSeconds) },
        },
      );
    }

    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return Response.json({ error: "Invalid request body." }, { status: 400 });
    }
    const payload = (body ?? {}) as Record<string, unknown>;

    const niche = String(payload.niche ?? "").trim().slice(0, 64);
    const keyword = String(payload.keyword ?? "").trim().slice(0, 120);
    if (!niche && !keyword) {
      return Response.json(
        { error: "Choose a niche or type a keyword." },
        { status: 400 },
      );
    }

    const supabase = await createClient();
    const tasks = new TaskRepository(supabase);
    if ((await tasks.activeCount("discover")) > 0) {
      return Response.json(
        { error: "A search is already running." },
        { status: 409 },
      );
    }

    const task = await tasks.create(user.id, "discover", {
      niche,
      keyword,
      language: String(payload.language ?? "").trim().slice(0, 8),
      max_age_days: Number(payload.maxAgeDays) || 30,
      max_results: Math.min(Math.max(Number(payload.maxResults) || 25, 1), 50),
      sort: String(payload.sort ?? "viral_score"),
    });
    return Response.json({ task }, { status: 202 });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
