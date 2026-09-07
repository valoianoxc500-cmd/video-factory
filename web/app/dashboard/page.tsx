import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { VideoRepository, JobRepository, ChannelRepository } from "@/lib/repositories";
import { VideoGrid } from "@/components/VideoGrid";

export const dynamic = "force-dynamic";

/** Channels that have their own still in /public/channels. */
const CHANNEL_ART = new Set(["horror_stories", "football_news"]);

/**
 * The Reels workspace, moved out of the sidebar.
 *
 * Same destinations, same order, with a line of explanation each -- which is
 * the thing a nav label could not carry and the reason the list was hard to
 * read there.
 */
const REELS_SECTIONS = [
  ["/dashboard/reels/saved", "Viral videos", "Saved from Discover", "M6 4h12v16l-6-4-6 4V4Z"],
  ["/dashboard/reels/videos", "My videos", "Yours, and their new versions", "M4 6h16M4 12h16M4 18h10"],
  ["/dashboard/reels/queue", "Queue", "Waiting to publish", "M4 6h16M4 12h16M4 18h16"],
  ["/dashboard/reels/scheduled", "Scheduled", "Posting at a set time", "M8 3v4m8-4v4M4 9h16M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z"],
  ["/dashboard/reels/published", "Published", "Already posted", "M5 13l4 4L19 7"],
  ["/dashboard/jobs", "Jobs & activity", "Every generation run", "M12 8v4l3 2m6-2a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"],
] as const;

export default async function OverviewPage() {
  const supabase = await createClient();
  const [videos, jobs, channels] = await Promise.all([
    new VideoRepository(supabase).listForUser(),
    new JobRepository(supabase).listForUser(10),
    new ChannelRepository(supabase).listForUser(),
  ]);

  const running = jobs.filter((j) => j.status === "queued" || j.status === "running").length;
  const totalSeconds = videos.reduce((sum, v) => sum + (v.duration_seconds ?? 0), 0);

  return (
    <>
      <div
        className="page-head"
        data-art="create"
        style={{ ["--head-art" as string]: "url('/channels/create.jpg')" }}
      >
        <h1>Overview</h1>
        <p>Everything generated in your workspace.</p>
      </div>

      <div className="stat-grid">
        <div className="stat"><div className="k">Videos</div><div className="v">{videos.length}</div></div>
        <div className="stat"><div className="k">Channels</div><div className="v">{channels.length}</div></div>
        <div className="stat"><div className="k">In progress</div><div className="v">{running}</div></div>
        <div className="stat"><div className="k">Total runtime</div><div className="v">{Math.round(totalSeconds / 60)}m</div></div>
      </div>

      {/*
        The nav used to list every channel and every Reels surface. It is a
        short list now, so the destinations live here instead -- with their
        counts, which a nav entry could never show as well.
      */}
      <div className="sec-head" style={{ marginTop: 30 }}>
        <h2>Channels</h2>
        <Link className="sec-link" href="/dashboard/create">
          Create a video
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path d="M5 12h14m-6-7 7 7-7 7" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </Link>
      </div>

      <div className="tile-grid">
        {channels.map((channel) => (
          <Link
            key={channel.slug}
            href={`/dashboard/channels/${channel.slug}`}
            className="tile tile-art"
            style={{
              ["--tile-art" as string]: `url('/channels/${
                CHANNEL_ART.has(channel.slug) ? channel.slug : "create"
              }.jpg')`,
            }}
          >
            <span className="tile-body">
              <strong>{channel.name}</strong>
              <small>
                {channel.videoCount} {channel.videoCount === 1 ? "video" : "videos"}
              </small>
            </span>
          </Link>
        ))}
      </div>

      <div className="sec-head" style={{ marginTop: 30 }}>
        <h2>Viral Reels Finder</h2>
        <Link className="sec-link" href="/dashboard/reels">
          Open Re-Create
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path d="M5 12h14m-6-7 7 7-7 7" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </Link>
      </div>

      <div className="tile-grid">
        {REELS_SECTIONS.map(([href, label, sub, d]) => (
          <Link key={href} href={href} className="tile">
            <span className="tile-icon" aria-hidden>
              <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
                <path d={d} stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
            <span className="tile-body">
              <strong>{label}</strong>
              <small>{sub}</small>
            </span>
          </Link>
        ))}
      </div>

      <div className="sec-head" style={{ marginTop: 30 }}>
        <h2>Recent videos</h2>
        <Link className="sec-link" href="/dashboard/library">
          View library
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path d="M5 12h14m-6-7 7 7-7 7" stroke="currentColor" strokeWidth="1.9" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </Link>
      </div>
      {videos.length === 0 ? (
        <div className="empty">
          <svg width="34" height="34" viewBox="0 0 24 24" fill="none" style={{ opacity: 0.35 }}>
            <path d="M4 5h16v14H4z" stroke="currentColor" strokeWidth="1.5" />
            <path d="m10 9 5 3-5 3V9Z" fill="currentColor" />
          </svg>
          <h3>No videos yet</h3>
          <p>Generate your first one and it will be saved here permanently.</p>
          <p style={{ marginTop: 16 }}>
            <Link href="/dashboard/create" className="btn-primary" style={{ display: "inline-block", width: "auto", padding: "9px 18px", textDecoration: "none" }}>
              Create a video
            </Link>
          </p>
        </div>
      ) : (
        <VideoGrid videos={videos.slice(0, 8)} />
      )}
    </>
  );
}
