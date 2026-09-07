import { createClient, requireUser } from "@/lib/supabase/server";
import { toHttpError } from "@/lib/repositories";
import { SourceRepository, TaskRepository } from "@/lib/vrf";

/**
 * Saved discoveries.
 *
 * These are other people's videos, kept as reference material: the row records
 * what was public about them and what they scored. Saving one grants no rights
 * to it, and nothing here can turn a saved source into something publishable —
 * that requires a rights attestation on the user's own upload.
 */

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    await requireUser();
    const supabase = await createClient();
    const sources = await new SourceRepository(supabase).listForUser();
    return Response.json({ sources });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message, sources: [] }, { status });
  }
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return Response.json({ error: "Invalid request body." }, { status: 400 });
    }
    const payload = (body ?? {}) as Record<string, unknown>;

    const supabase = await createClient();
    const source = await new SourceRepository(supabase).save(user.id, payload);

    // Analysis reads public metadata and the thumbnail only; it never copies
    // the video. viral/analysis.py enforces that from the rights basis.
    if (payload.analyse) {
      await new TaskRepository(supabase).create(user.id, "analyse", {
        source_id: source.id,
      });
    }
    return Response.json({ source }, { status: 201 });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
