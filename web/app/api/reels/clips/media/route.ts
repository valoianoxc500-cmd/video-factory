import { createClient, requireUser } from "@/lib/supabase/server";
import { AssetRepository, toReelsHttpError } from "@/lib/vrf";
import { FederationError, UploadConfigError, signedReadUrl } from "@/lib/uploads";
import { reportFederationFailure } from "@/lib/gcs-auth";

/**
 * A short-lived URL for one Clipping video the caller owns.
 *
 * Clipping used to put the stored object URL straight into the player and the
 * download link, which worked only because the bucket is world-readable —
 * every uploaded video was fetchable by anyone holding the path. This mints a
 * signed, expiring URL instead, so a link copied out of the page stops
 * working and the bucket does not have to stay public for Clipping's sake.
 *
 * Ownership is checked before anything is signed: the asset is read through
 * the caller's own session, so RLS turns another user's id into a miss and
 * there is no separate ownership branch to get wrong. Signing is keyless —
 * the same federated credential the upload path uses.
 */

export const dynamic = "force-dynamic";

function fail(err: unknown) {
  if (err instanceof FederationError) {
    reportFederationFailure(err);
    return Response.json(
      { error: "That video is not available right now." },
      { status: err.isConfig ? 503 : 502 },
    );
  }
  if (err instanceof UploadConfigError) {
    console.error(`[clip-media] ${err.message}`);
    return Response.json(
      { error: "That video is not available right now." },
      { status: 503 },
    );
  }
  const { status, message } = toReelsHttpError(err);
  return Response.json({ error: message }, { status });
}

export async function GET(request: Request) {
  try {
    await requireUser();
    const oidcToken = request.headers.get("x-vercel-oidc-token") ?? undefined;
    const url = new URL(request.url);
    const assetId = String(url.searchParams.get("asset") ?? "").trim();
    const kind = String(url.searchParams.get("kind") ?? "source").trim();

    if (!assetId) {
      return Response.json({ error: "Choose a video." }, { status: 400 });
    }
    if (kind !== "source" && kind !== "processed") {
      return Response.json({ error: "Unknown media type." }, { status: 400 });
    }

    const supabase = await createClient();
    const asset = await new AssetRepository(supabase).get(assetId);

    const stored =
      kind === "processed"
        ? String(asset.processed_path ?? "")
        : String(asset.storage_path ?? "");
    if (!stored.trim()) {
      return Response.json(
        { error: "That video has no file to play yet." },
        { status: 409 },
      );
    }

    const signed = await signedReadUrl(stored, undefined, oidcToken);
    return Response.json(signed);
  } catch (err) {
    return fail(err);
  }
}
