import { createClient, requireUser } from "@/lib/supabase/server";
import { AssetRepository, TaskRepository, toReelsHttpError } from "@/lib/vrf";

/**
 * The user's own videos: the only things this product will publish.
 *
 * Adding one takes a link and one confirmation -- that the user owns the
 * content or has permission to reuse it. Platform, video id and whether the
 * media can legitimately be fetched are all derived from the URL server-side.
 *
 * The rights gate is unchanged in substance: something added here carries an
 * explicit attestation, and `discovered` is still not a rights basis, here or
 * in the table's CHECK constraint. Adding a video from Discover uses this same
 * route and so requires the same confirmation -- finding a video is not a
 * reason to publish it.
 */

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    await requireUser();
    const supabase = await createClient();
    const assets = await new AssetRepository(supabase).listForUser();
    return Response.json({ assets });
  } catch (err) {
    const { status, message } = toReelsHttpError(err);
    return Response.json({ error: message, assets: [] }, { status });
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

    // Which platforms this user has actually authorised. Read here rather than
    // taken from the request: whether media may be fetched depends on it, so a
    // client claiming a connection it does not have would otherwise decide its
    // own import permissions.
    // A live connection is a row with no revoked_at; there is no `status`
    // column. Selecting one that does not exist made PostgREST reject the
    // query, which left this list empty and quietly classified every
    // owner-importable link as metadata-only.
    const { data: accountRows, error: accountsError } = await supabase
      .from("vrf_accounts")
      .select("platform, revoked_at")
      .is("revoked_at", null);
    if (accountsError) throw new Error(accountsError.message);
    const connectedPlatforms = (accountRows ?? []).map((row) =>
      String((row as { platform: string }).platform),
    );

    const asset = await new AssetRepository(supabase).createFromUrl(user.id, {
      url: String(payload.url ?? payload.sourceUrl ?? ""),
      ownsOrPermitted: payload.ownsOrPermitted === true,
      connectedPlatforms,
      title: String(payload.title ?? ""),
      thumbnailUrl: String(payload.thumbnailUrl ?? ""),
      author: String(payload.author ?? ""),
    });

    // Analyse first -- the Original Viral Score is derived from the source's
    // own public metrics and does not need the media file, so it runs even for
    // platforms that will only ever give us metadata.
    const tasks = new TaskRepository(supabase);
    await tasks.create(user.id, "analyse", { asset_id: asset.id });

    // Import the file from the connected account that published it, then do
    // the format and quality work: re-encode, correct the aspect ratio,
    // normalise loudness. Nothing that alters the content.
    //
    // One task, because the processing needs the file the import fetches --
    // queuing them separately would leave the second waiting on a file that a
    // failed import never produced. Skipped entirely for a metadata-only
    // asset, which will never have a file to work on.
    if (asset.ingest_status !== "metadata_only") {
      await tasks.create(user.id, "ingest", {
        asset_id: asset.id,
        platform: String(payload.platform ?? "tiktok"),
      });
    }

    return Response.json({ asset }, { status: 201 });
  } catch (err) {
    const { status, message } = toReelsHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
