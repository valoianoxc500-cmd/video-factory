import { createClient } from "@/lib/supabase/server";
import { VideoRepository } from "@/lib/repositories";
import { VideoGrid } from "@/components/VideoGrid";

export const dynamic = "force-dynamic";

export default async function LibraryPage() {
  const supabase = await createClient();
  const videos = await new VideoRepository(supabase).listForUser();

  return (
    <>
      <div
        className="page-head"
        data-art="library"
        style={{ ["--head-art" as string]: "url('/channels/library.jpg')" }}
      >
        <h1>Video Library</h1>
        <p>{videos.length} saved {videos.length === 1 ? "video" : "videos"} across every channel.</p>
      </div>
      {videos.length === 0 ? (
        <div className="empty">
          <h3>Your library is empty</h3>
          <p>Finished videos are stored permanently and stay here across deployments and restarts.</p>
        </div>
      ) : (
        <VideoGrid videos={videos} />
      )}
    </>
  );
}
