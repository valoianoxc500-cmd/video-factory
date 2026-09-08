import { createClient, requireUser } from "@/lib/supabase/server";
import { JobRepository, toHttpError } from "@/lib/repositories";
import { rateLimit } from "@/lib/rate-limit";

/**
 * The caller's own jobs.
 *
 * Identity comes from the verified session; a user_id in the request body is
 * ignored. RLS scopes every read and write to that identity, so this route
 * cannot list or create work for another account even if it tried.
 */

export const dynamic = "force-dynamic";
export const maxDuration = 30;

export async function GET() {
  try {
    await requireUser();
    const supabase = await createClient();
    const repo = new JobRepository(supabase);
    // Recover abandoned jobs on the read the dashboard is already making, so
    // a job whose worker died stops reading as "in progress" forever.
    await repo.expireStale();
    const jobs = await repo.listForUser();
    return Response.json({ jobs });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message, jobs: [] }, { status });
  }
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();

    const limited = rateLimit(`jobs:create:${user.id}`, {
      limit: 10,
      windowSeconds: 60 * 10,
    });
    if (!limited.allowed) {
      return Response.json(
        { error: "Too many generations started. Try again shortly." },
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

    const supabase = await createClient();
    const jobs = new JobRepository(supabase);

    // Clear abandoned jobs first. Without this a job whose worker died stays
    // "in progress" forever and the 409 below locks the account out of
    // starting anything new.
    await jobs.expireStale();

    // One generation at a time per account: a run takes minutes and the worker
    // is single-instance, so a second queued job would only look stuck.
    const active = await jobs.activeForUser();
    if (active) {
      return Response.json(
        { error: "A generation is already in progress.", activeJobId: active.id },
        { status: 409 },
      );
    }

    const job = await jobs.create({
      userId: user.id,
      topic: String(payload.topic ?? ""),
      channel: String(payload.engine ?? payload.channel ?? ""),
      style: payload.style ? String(payload.style) : undefined,
      language: payload.language ? String(payload.language) : undefined,
      captionLanguage: payload.captionLanguage
        ? String(payload.captionLanguage)
        : undefined,
    });
    return Response.json(job, { status: 201 });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
