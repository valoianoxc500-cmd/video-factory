import { createClient, requireUser } from "@/lib/supabase/server";
import { VideoRepository, toHttpError, NotFoundError } from "@/lib/repositories";
import { mediaUrl } from "@/lib/media";

/**
 * Mint a short-lived URL for one video or thumbnail.
 *
 * Ownership is checked before anything is signed: the video is fetched through
 * the caller's own session, so RLS turns another user's id into a miss. Only
 * then is a URL produced, and it expires -- so a shared link stops working
 * rather than granting permanent access to a private object.
 */

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  context: { params: Promise<{ videoId: string; kind: string }> },
) {
  try {
    const { videoId, kind } = await context.params;
    if (kind !== "video" && kind !== "thumbnail") {
      return Response.json({ error: "Unknown media type." }, { status: 400 });
    }
    if (!/^[0-9a-f-]{36}$/i.test(videoId)) {
      return Response.json({ error: "Not found." }, { status: 404 });
    }

    await requireUser();
    const supabase = await createClient();
    const video = await new VideoRepository(supabase).getOwned(videoId);

    const path = kind === "video" ? video.video_path : video.thumbnail_path;
    if (!path) throw new NotFoundError("No thumbnail for that video.");

    const { url, mode, expiresInSeconds } = mediaUrl(path);
    return Response.json({ url, mode, expiresInSeconds });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message }, { status });
  }
}
