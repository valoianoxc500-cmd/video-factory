import { notFound } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { VideoRepository, CHANNELS, isKnownChannel } from "@/lib/repositories";
import { VideoGrid } from "@/components/VideoGrid";

export const dynamic = "force-dynamic";

export default async function ChannelPage({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  if (!isKnownChannel(slug)) notFound();
  // Channels with their own artwork in /public/channels. Anything else takes
  // the neutral still rather than requesting a file that is not there.
  const CHANNEL_ART_SLUGS = new Set(["horror_stories", "football_news"]);

  const channel = CHANNELS.find((c) => c.slug === slug)!;
  const supabase = await createClient();
  const videos = await new VideoRepository(supabase).listForUser(slug);

  return (
    <>
      {/* Each channel's own still, so the page reads as that channel before
          the heading is. Falls back to the neutral one for a channel that has
          no artwork yet. */}
      <div
        className="page-head"
        data-art={slug}
        style={{
          ["--head-art" as string]:
            `url('/channels/${CHANNEL_ART_SLUGS.has(slug) ? slug : "create"}.jpg')`,
        }}
      >
        <h1>{channel.name}</h1>
        <p>{videos.length} {videos.length === 1 ? "video" : "videos"} in this channel.</p>
      </div>
      {videos.length === 0 ? (
        <div className="empty">
          <h3>Nothing here yet</h3>
          <p>Videos generated on {channel.name} will appear here. Each channel keeps its own separate library.</p>
        </div>
      ) : (
        <VideoGrid videos={videos} />
      )}
    </>
  );
}
