import { createClient, requireUser } from "@/lib/supabase/server";
import { toHttpError } from "@/lib/repositories";
import { PublishJobRepository } from "@/lib/vrf";

/** The caller's publish jobs, optionally filtered by status. */

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  try {
    await requireUser();
    const status = new URL(request.url).searchParams.get("status") ?? "";
    const supabase = await createClient();
    const repository = new PublishJobRepository(supabase);

    const jobs =
      status === "scheduled"
        ? await repository.scheduled()
        : await repository.listForUser(status || undefined);
    return Response.json({ jobs });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message, jobs: [] }, { status });
  }
}
