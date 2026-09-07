import { createClient, requireUser } from "@/lib/supabase/server";
import { toHttpError } from "@/lib/repositories";
import { AccountRepository, AssetRepository, PublishJobRepository } from "@/lib/vrf";
import { planPublish, validatePublish } from "@/lib/vrf-publish";

/**
 * Queue a publish or a scheduled post.
 *
 * Three checks run before anything is queued: the request itself (platforms,
 * schedule), the asset's rights basis, and whether the user has actually
 * connected each platform. Queuing a job that is certain to fail just moves
 * the bad news somewhere the user is not looking.
 */

export const dynamic = "force-dynamic";
export const maxDuration = 30;

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

    const input = {
      assetId: String(payload.assetId ?? ""),
      caption: String(payload.caption ?? ""),
      platforms: Array.isArray(payload.platforms)
        ? payload.platforms.map((p) => String(p))
        : [],
      scheduledFor: payload.scheduledFor ? String(payload.scheduledFor) : null,
      attribution: String(payload.attribution ?? ""),
    };

    const problems = validatePublish(input);
    if (problems.length > 0) {
      return Response.json({ error: problems[0], problems }, { status: 400 });
    }

    const supabase = await createClient();
    const asset = await new AssetRepository(supabase).get(input.assetId);
    if (!asset.processed_path) {
      return Response.json(
        { error: "This video is still being processed." },
        { status: 409 },
      );
    }

    // Creative Commons material carries a credit obligation, so the caption
    // has to carry the credit.
    const attribution =
      asset.needs_attribution && asset.rights_holder
        ? input.attribution || `Credit: ${asset.rights_holder}`
        : input.attribution;
    if (asset.needs_attribution && !attribution.trim()) {
      return Response.json(
        {
          error:
            "This video is Creative Commons; the caption must credit the " +
            "rights holder.",
        },
        { status: 400 },
      );
    }

    const connected = new Set(
      (await new AccountRepository(supabase).listForUser()).map((a) => a.platform),
    );
    const missing = input.platforms.filter((p) => !connected.has(p));
    if (missing.length > 0) {
      return Response.json(
        {
          error: `Connect ${missing.join(" and ")} in Connected Accounts first.`,
          missing,
        },
        { status: 409 },
      );
    }

    const planned = planPublish({ ...input, attribution });
    const jobs = await new PublishJobRepository(supabase).createMany(
      user.id,
      asset.id,
      planned.map((job) => ({ ...job })),
    );
    return Response.json({ jobs }, { status: 202 });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
