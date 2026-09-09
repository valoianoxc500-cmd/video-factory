import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { VideoRepository, JobRepository, ChannelRepository } from "@/lib/repositories";
import { VideoGrid } from "@/components/VideoGrid";

export const dynamic = "force-dynamic";

/**
 * The workspace overview.
 *
 * Three questions, in the order someone actually has them: what is the state
 * of my work, what do I want to make, and what have I made. The channel cards
 * carry their own photograph and their own colour, so the thing you came to do
 * is recognisable before you have read its label.
 */

/** Where you make something. Each has its own still and its own accent. */
const MAKE = [
  {
    href: "/dashboard/football",
    surface: "football",
    art: "football_news.jpg",
    kicker: "Football",
    title: "Football News",
    blurb: "Researched against live squad and transfer data, then written, narrated and illustrated.",
  },
  {
    href: "/dashboard/story/horror",
    surface: "horror",
    art: "horror_stories.jpg",
    kicker: "Story To Video",
    title: "Horror Stories",
    blurb: "Paranormal accounts, urban legends and original horror, in one unmistakable voice.",
  },
  {
    href: "/dashboard/story/true",
    surface: "true",
    art: "create.jpg",
    kicker: "Story To Video",
    title: "True Stories",
    blurb: "Real cases. Verified facts stated plainly, claims attributed, nothing invented.",
  },
  {
    href: "/dashboard/animated",
    surface: "animated",
    art: "create.jpg",
    kicker: "Story To Video",
    title: "Animated Stories",
    blurb: "Build a consistent visual world around one character and let each scene carry the story forward.",
  },
  {
    href: "/dashboard/quotes",
    surface: "quotes",
    art: "library.jpg",
    kicker: "Editorial images",
    title: "Quote Studio",
    blurb: "Turn a thought into a refined, image-only quote carousel that is ready to share.",
  },
  {
    href: "/dashboard/clipping",
    surface: "clipping",
    art: "reels.jpg",
    kicker: "Your footage",
    title: "Clipping",
    blurb: "Cut a section out of a video you own and reframe it to 9:16.",
  },
] as const;

/** Where you study or manage what exists. */
const WORK = [
  ["/dashboard/analyzer", "analyzer", "Clip Analyzer", "Why a video performed", "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm10 17-5.2-5.2M8.5 11h5M11 8.5v5"],
  ["/dashboard/reels", "", "Find Viral", "What is performing right now", "M21 21l-4.3-4.3M11 19a8 8 0 1 1 0-16 8 8 0 0 1 0 16Z"],
  ["/dashboard/reels/videos", "", "My Videos", "Everything you have imported", "M4 6h11a4 4 0 0 1 0 8H7m0 0 3-3m-3 3 3 3M4 4v4h4"],
  ["/dashboard/reels/analytics", "analytics", "Analytics", "How your posts performed", "M4 20V10m6 10V4m6 16v-7"],
  ["/dashboard/library", "library", "Library", "Every finished video", "M4 6h16M4 12h16M4 18h10"],
  ["/dashboard/jobs", "", "Jobs & activity", "Every generation run", "M12 8v4l3 2m6-2a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z"],
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
        className="hero"
        style={{ ["--head-art" as string]: "url('/channels/create.jpg')" }}
      >
        <h1>
          Turn a story into a <span className="hl">finished video</span>
        </h1>
        <p>
          Researched against real sources, narrated in a real voice, and cut
          with real footage — end to end, without you touching a timeline.
        </p>
        <div className="hero-actions">
          <Link href="/dashboard/story/horror" className="btn-primary">
            Create a video
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path d="M5 12h14m-6-7 7 7-7 7" stroke="currentColor" strokeWidth="2.1" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </Link>
          <Link href="/dashboard/analyzer" className="btn-ghost">
            Analyse a video
          </Link>
        </div>
      </div>

      <div className="stat-grid">
        <div className="stat"><div className="k">Videos</div><div className="v">{videos.length}</div></div>
        <div className="stat"><div className="k">Channels</div><div className="v">{channels.length}</div></div>
        <div className="stat"><div className="k">In progress</div><div className="v">{running}</div></div>
        <div className="stat"><div className="k">Total runtime</div><div className="v">{Math.round(totalSeconds / 60)}m</div></div>
      </div>

      <div className="sec-head">
        <h2>Make something</h2>
      </div>

      <div className="chan-cards">
        {MAKE.map((m) => (
          <Link
            key={m.href}
            href={m.href}
            className="chan-card"
            data-surface={m.surface}
            style={{ ["--chan-art" as string]: `url('/channels/${m.art}')` }}
          >
            <span className="chan-badge">{m.kicker}</span>
            <span className="chan-go" aria-hidden>
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
                <path d="m9 5 7 7-7 7" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </span>
            <h3>{m.title}</h3>
            <p>{m.blurb}</p>
          </Link>
        ))}
      </div>

      <div className="sec-head">
        <h2>Study and manage</h2>
      </div>

      <div className="tile-grid">
        {WORK.map(([href, surface, label, sub, d]) => (
          <Link key={href} href={href} className="tile" data-surface={surface || undefined}>
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

      <div className="sec-head">
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
          <p style={{ marginTop: 18 }}>
            <Link href="/dashboard/story/horror" className="btn-primary">
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
