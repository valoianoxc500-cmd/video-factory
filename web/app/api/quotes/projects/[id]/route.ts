import { createClient, requireUser } from "@/lib/supabase/server";
import { QuoteStudioRepository } from "@/lib/repositories";
import { normaliseBackground } from "@/lib/quotes";

/**
 * One Quote Studio project: read and update.
 *
 * Reading first is the authorisation check. RLS scopes the repository to the
 * signed-in user, so a project belonging to somebody else is simply not
 * found -- there is no separate ownership branch to get wrong.
 */

export const dynamic = "force-dynamic";

function fail(err: unknown) {
  const message = (err as Error)?.message ?? "";
  const name = (err as Error)?.name ?? "";
  if (/sign|auth|session/i.test(message)) {
    return Response.json({ error: "You must be signed in." }, { status: 401 });
  }
  if (name === "NotFoundError") {
    return Response.json({ error: message }, { status: 404 });
  }
  if (name === "ValidationError") {
    return Response.json({ error: message }, { status: 400 });
  }
  return Response.json({ error: "Could not save that project." }, { status: 500 });
}

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { id } = await params;
    const user = await requireUser();
    const db = await createClient();
    const project = await new QuoteStudioRepository(db, user.id).getProject(id);
    return Response.json({ project });
  } catch (err) {
    return fail(err);
  }
}

export async function PATCH(
  request: Request,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { id } = await params;
    const user = await requireUser();
    const db = await createClient();

    let body: Record<string, unknown>;
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      return Response.json({ error: "Invalid request body." }, { status: 400 });
    }

    const store = new QuoteStudioRepository(db, user.id);
    const existing = await store.getProject(id);

    const project = await store.updateProject(id, {
      quotes: (body.quotes ?? existing.quotes) as never,
      fontId: String(body.fontId ?? existing.fontId),
      ratio: body.ratio === "9:16" ? "9:16" : "4:5",
      // An update that does not mention a background keeps the saved one
      // rather than resetting the deck to white.
      backgroundId: normaliseBackground(body.backgroundId ?? existing.backgroundId),
    });

    return Response.json({ project });
  } catch (err) {
    return fail(err);
  }
}
