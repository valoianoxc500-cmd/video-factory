import { createClient, requireUser } from "@/lib/supabase/server";
import { toHttpError } from "@/lib/repositories";
import { SourceRepository } from "@/lib/vrf";

/** Remove one saved discovery. RLS makes another user's id a no-op. */

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
    await new SourceRepository(supabase).remove(id);
    return Response.json({ ok: true });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
