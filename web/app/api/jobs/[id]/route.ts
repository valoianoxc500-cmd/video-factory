import { createClient, requireUser } from "@/lib/supabase/server";
import { JobRepository, toHttpError } from "@/lib/repositories";

/** One job, only if the caller owns it. RLS turns another user id into a 404. */

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  try {
    const { id } = await context.params;
    if (!/^[0-9a-f-]{36}$/i.test(id)) {
      return Response.json({ error: "Not found." }, { status: 404 });
    }
    await requireUser();
    const supabase = await createClient();
    const repo = new JobRepository(supabase);
    // This is the endpoint the progress view polls, so it is where a stuck
    // job has to be noticed.
    await repo.expireStale();
    return Response.json(await repo.getOwned(id));
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
