import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { VideoRepository } from "@/lib/repositories";
import { VideoGrid } from "@/components/VideoGrid";

export const dynamic = "force-dynamic";

/** The full-width buttons need shrinking when two sit side by side. */
const INLINE_CTA = {
  display: "inline-block",
  width: "auto",
  padding: "9px 18px",
  textDecoration: "none",
} as const;

export default async function LibraryPage() {
  const supabase = await createClient();
  const videos = await new VideoRepository(supabase).listForUser();

  return (
    <>
      <div
        className="page-head"
        data-art="library"
        data-surface="library"
        style={{ ["--head-art" as string]: "url('/channels/library.jpg')" }}
      >
        <h1>
          Video <span className="hl">library</span>
        </h1>
        <p>{videos.length} saved {videos.length === 1 ? "video" : "videos"} across every channel.</p>
      </div>
      {videos.length === 0 ? (
        <div className="empty">
          <h3>Your library is empty</h3>
          <p>
            Finished videos are stored permanently and stay here across
            deployments and restarts. Nothing has finished yet — start one and
            it will land here.
          </p>
          {/* An empty state that offers no way out is a dead end. */}
          <p style={{ marginTop: 18, display: "flex", gap: 10, justifyContent: "center" }}>
            <Link href="/dashboard/create" className="btn-primary" style={INLINE_CTA}>
              Create a video
            </Link>
            <Link href="/dashboard/reels/videos" className="btn-ghost" style={INLINE_CTA}>
              My Videos
            </Link>
          </p>
        </div>
      ) : (
        <VideoGrid videos={videos} />
      )}
    </>
  );
}
