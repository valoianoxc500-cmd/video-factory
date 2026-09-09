import { createClient, requireUser } from "@/lib/supabase/server";
import { QuoteStudioRepository } from "@/lib/repositories";
import { normaliseBackground, validLanguage } from "@/lib/quotes";

/**
 * Quote Studio projects: list and create.
 *
 * The background is part of the project, not a view setting, which is why it
 * is written here rather than kept in the browser: a deck reopened next month
 * has to look like the deck that was designed, and a surface chosen once
 * should not have to be chosen again.
 */

export const dynamic = "force-dynamic";

function repo(db: Awaited<ReturnType<typeof createClient>>, userId: string) {
  return new QuoteStudioRepository(db, userId);
}

function fail(err: unknown) {
  const message = (err as Error)?.message ?? "";
  if (/sign|auth|session/i.test(message)) {
    return Response.json({ error: "You must be signed in." }, { status: 401 });
  }
  // Validation messages are written for the person reading them; everything
  // else is reported generically rather than leaking a database error.
  const isValidation = (err as Error)?.name === "ValidationError";
  return Response.json(
    { error: isValidation ? message : "Could not save that project." },
    { status: isValidation ? 400 : 500 },
  );
}

export async function GET() {
  try {
    const user = await requireUser();
    const db = await createClient();
    const projects = await repo(db, user.id).listProjects();
    return Response.json({ projects });
  } catch (err) {
    return fail(err);
  }
}

export async function POST(request: Request) {
  try {
    const user = await requireUser();
    const db = await createClient();

    let body: Record<string, unknown>;
    try {
      body = (await request.json()) as Record<string, unknown>;
    } catch {
      return Response.json({ error: "Invalid request body." }, { status: 400 });
    }

    const language = body.language;
    if (!validLanguage(language)) {
      return Response.json({ error: "Choose Arabic or English." }, { status: 400 });
    }

    const store = repo(db, user.id);

    // The profile travels with the project so the card keeps the face and
    // handle it was designed with, even if the profile changes later.
    const profile = body.profile as Record<string, unknown> | undefined;
    if (profile) {
      await store.saveProfile({
        photo: String(profile.photo ?? ""),
        displayName: String(profile.displayName ?? ""),
        username: String(profile.username ?? ""),
        preferredLanguage: language,
      });
    }

    const project = await store.createProject({
      topic: String(body.topic ?? ""),
      language,
      fontId: String(body.fontId ?? ""),
      ratio: body.ratio === "9:16" ? "9:16" : "4:5",
      backgroundId: normaliseBackground(body.backgroundId),
      quotes: (body.quotes ?? []) as never,
    });

    return Response.json({ project }, { status: 201 });
  } catch (err) {
    return fail(err);
  }
}
