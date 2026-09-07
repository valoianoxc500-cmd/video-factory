import { createClient, requireUser } from "@/lib/supabase/server";
import { toHttpError } from "@/lib/repositories";
import { PublishJobRepository } from "@/lib/vrf";

/**
 * Cancel a queued or in-flight publish.
 *
 * A job that already posted is left alone: cancelling here would only change
 * a row, not unpublish anything, and saying "cancelled" about a live post
 * would be a lie.
 */

export const dynamic = "force-dynamic";

export async function DELETE(
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
    const job = await new PublishJobRepository(supabase).cancel(id);
    return Response.json({ job });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
