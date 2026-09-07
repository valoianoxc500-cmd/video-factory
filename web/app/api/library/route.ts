/**
 * Permanent video library, grouped by channel.
 *
 * This route previously read a public index.json straight from the bucket and
 * required no session, which predates accounts: it returned every video's
 * durable public URL to anyone who asked. It now answers from the `videos`
 * table through the same repository as everything else, so a caller sees only
 * their own rows and gets records rather than media URLs -- a playable URL is
 * minted per request by /api/media/[videoId]/[kind].
 *
 * /api/videos returns a flat list; this returns the same records already
 * grouped, which is what a per-channel view needs.
 */

import { createClient, requireUser } from "@/lib/supabase/server";
import {
  CHANNELS,
  VideoRepository,
  toHttpError,
  type VideoRow,
} from "@/lib/repositories";

export const dynamic = "force-dynamic";
export const maxDuration = 30;

export async function GET(request: Request) {
  try {
    // Identity first: an unauthenticated caller should be told to sign in,
    // not told whether the channel they guessed was spelled correctly.
    await requireUser();
    const supabase = await createClient();

    const requested = new URL(request.url).searchParams.get("channel") ?? "";
    const videos = await new VideoRepository(supabase).listForUser(
      requested || undefined,
    );

    const grouped = CHANNELS.filter(
      (channel) => !requested || channel.slug === requested,
    ).map((channel) => ({
      slug: channel.slug,
      name: channel.name,
      theme: channel.theme,
      videos: videos.filter((v: VideoRow) => v.channel_slug === channel.slug),
    }));

    return Response.json({ channels: grouped, videos });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message, channels: [], videos: [] }, { status });
  }
}
