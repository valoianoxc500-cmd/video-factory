import { createClient, requireUser } from "@/lib/supabase/server";
import { VideoRepository, toHttpError } from "@/lib/repositories";

/**
 * The caller's video library, optionally one channel.
 *
 * Returns records only -- no media URLs. A playable URL is minted per request
 * by /api/media/[videoId]/[kind], so nothing here can become a permanent link
 * to a private object.
 */

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  try {
    await requireUser();
    const supabase = await createClient();
    const channel = new URL(request.url).searchParams.get("channel") ?? undefined;
    const videos = await new VideoRepository(supabase).listForUser(
      channel || undefined,
    );
    return Response.json({ videos });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message, videos: [] }, { status });
  }
}
