import { createClient, requireUser } from "@/lib/supabase/server";
import { toHttpError } from "@/lib/repositories";
import { AccountRepository } from "@/lib/vrf";

/**
 * Disconnect an account.
 *
 * The row is marked revoked and its token material is cleared, so a leaked
 * backup cannot be replayed. RLS means another user's id changes nothing.
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
    await new AccountRepository(supabase).revoke(id);
    return Response.json({ ok: true });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
