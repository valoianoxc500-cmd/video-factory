import { createClient, requireUser } from "@/lib/supabase/server";
import { DEFAULT_SETTINGS, sanitiseSettings } from "@/lib/aivideo";

/**
 * The customer's saved AI Video Maker defaults.
 *
 * Read once when the page mounts and written by "Save as default". Everything
 * is sanitised on the way in and on the way out, so a row written by an older
 * build — or by a build that had one fewer control — still restores cleanly
 * instead of leaving the form in a half-configured state.
 */

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    const user = await requireUser();
    const db = await createClient();
    const { data, error } = await db
      .from("ai_video_prefs")
      .select("settings, named_presets")
      .eq("user_id", user.id)
      .maybeSingle();
    if (error) throw new Error(error.message);

    return Response.json({
      settings: data?.settings ? sanitiseSettings(data.settings) : DEFAULT_SETTINGS,
      presets: Array.isArray(data?.named_presets) ? data.named_presets : [],
    });
  } catch (err) {
    const message = (err as Error)?.message ?? "";
    if (/sign|auth|session|jwt/i.test(message)) {
      return Response.json({ error: "You must be signed in." }, { status: 401 });
    }
    // Defaults are a convenience: if they cannot be read, the form should
    // still open on something sensible rather than showing an error.
    console.warn("aivideo prefs read failed:", message);
    return Response.json({ settings: DEFAULT_SETTINGS, presets: [] });
  }
}

export async function PUT(request: Request) {
  try {
    const user = await requireUser();
    const body = await request.json().catch(() => ({}));
    const settings = sanitiseSettings(body?.settings);

    const presets = Array.isArray(body?.presets)
      ? body.presets
          .slice(0, 12)
          .map((p: { name?: unknown; settings?: unknown }) => ({
            name: String(p?.name ?? "").slice(0, 60),
            settings: sanitiseSettings(p?.settings),
          }))
          .filter((p: { name: string }) => p.name.length > 0)
      : undefined;

    const db = await createClient();
    const row: Record<string, unknown> = {
      user_id: user.id,
      settings,
      updated_at: new Date().toISOString(),
    };
    if (presets) row.named_presets = presets;

    const { error } = await db
      .from("ai_video_prefs")
      .upsert(row, { onConflict: "user_id" });
    if (error) throw new Error(error.message);

    return Response.json({ settings, presets: presets ?? undefined });
  } catch (err) {
    const message = (err as Error)?.message ?? "";
    if (/sign|auth|session|jwt/i.test(message)) {
      return Response.json({ error: "You must be signed in." }, { status: 401 });
    }
    console.error("aivideo prefs save failed:", message);
    return Response.json({ error: "Could not save your defaults." }, { status: 500 });
  }
}
