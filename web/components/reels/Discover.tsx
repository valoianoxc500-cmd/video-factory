"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";

/**
 * Trending-video search.
 *
 * A search is a task the worker runs, so this posts one and polls it.
 *
 * The primary action on a result is Re Create: confirm you may use it, and a
 * new version is made and saved to My Videos. There is nothing else to fill
 * in -- the processing is fixed (re-encode, reframe to 9:16, normalise
 * loudness) and the scores are results, not settings.
 *
 * The one thing that is not one click is the rights confirmation, and it stays
 * that way. These are other people's videos; discovering one says nothing
 * about who may use it. Nothing here publishes anywhere.
 */

interface Discovered {
  platform: string;
  video_id: string;
  url: string;
  title: string;
  author: string;
  thumbnail_url: string;
  views: number | null;
  likes: number | null;
  followers: number | null;
  duration_seconds: number | null;
  viral_score: number | null;
  score_confidence: string;
}

interface Task {
  id: string;
  status: "queued" | "running" | "done" | "failed";
  payload: Record<string, unknown>;
  result: { videos?: Discovered[]; providers?: ProviderStatus[]; note?: string };
  error: string;
}

interface ProviderStatus {
  platform: string;
  availability: string;
  reason: string;
}

const NICHES = [
  ["police_chase", "Police chase"],
  ["football", "Football"],
  ["cars", "Cars"],
  ["stories", "Stories"],
  ["fitness", "Fitness"],
  ["business", "Business"],
  ["animals", "Animals"],
  ["food", "Food"],
] as const;

const SORTS = [
  ["viral_score", "Viral score"],
  ["views", "Most viewed"],
  ["fastest_growing", "Fastest growing"],
  ["newest", "Newest"],
] as const;

export function Discover({ initial }: { initial: Task | null }) {
  const [task, setTask] = useState<Task | null>(initial);
  const [niche, setNiche] = useState<string>("police_chase");
  const [keyword, setKeyword] = useState("");
  const [sort, setSort] = useState<string>("viral_score");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState<string>("");
  const [saved, setSaved] = useState<Set<string>>(new Set());
  const [claiming, setClaiming] = useState<Discovered | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const router = useRouter();

  const running = task?.status === "queued" || task?.status === "running";

  /**
   * A queued task means nothing has picked it up yet.
   *
   * Discovery is run by the Python worker, not by the request handler, so if
   * that worker is not running the row sits at `queued` forever and the page
   * polls it forever. That is what "stuck on Searching…" was: not a slow
   * search, but a search nobody was listening for.
   *
   * Waiting a bounded time and then saying so is the difference between a
   * spinner and a diagnosis.
   */
  const UNCLAIMED_TIMEOUT_MS = 90_000;
  const RUNNING_TIMEOUT_MS = 5 * 60_000;

  const waitingSince = useRef<number | null>(null);
  const [stalled, setStalled] = useState("");

  const poll = useCallback(async () => {
    try {
      const response = await fetch("/api/reels/discover", { cache: "no-store" });
      if (!response.ok) {
        // 401 here means the session expired mid-search; anything else is the
        // API failing. Either way, stop pretending a search is in progress.
        setStalled(
          response.status === 401
            ? "Your session expired. Sign in again to search."
            : `The search could not be checked (HTTP ${response.status}).`,
        );
        return;
      }
      const body = await response.json();
      if (body.task) setTask(body.task as Task);
    } catch {
      // A dropped connection should not silently end polling, which is what
      // an unhandled rejection here used to do.
      setStalled("Lost connection while checking the search. Try again.");
    }
  }, []);

  useEffect(() => {
    if (!running) {
      waitingSince.current = null;
      return;
    }
    if (waitingSince.current === null) waitingSince.current = Date.now();

    const elapsed = Date.now() - waitingSince.current;
    const limit =
      task?.status === "running" ? RUNNING_TIMEOUT_MS : UNCLAIMED_TIMEOUT_MS;

    if (elapsed > limit) {
      setStalled(
        task?.status === "running"
          ? "The search started but did not finish. The worker may have stopped " +
            "partway; try again."
          : "No worker picked up this search. Discovery runs on the Viral " +
            "Reels Finder worker (vrf_worker.py) — if it is not running, " +
            "searches queue up and never start.",
      );
      return;
    }

    timer.current = setTimeout(poll, 2000);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [running, task, poll]);

  async function search(event: React.FormEvent) {
    event.preventDefault();
    setError("");
    setStalled("");
    waitingSince.current = null;
    const response = await fetch("/api/reels/discover", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ niche, keyword, sort }),
    });
    const body = await response.json();
    if (!response.ok) {
      setError(body.error ?? "The search could not be started.");
      return;
    }
    setTask(body.task as Task);
  }

  async function save(video: Discovered, analyse: boolean) {
    setSaving(video.video_id);
    try {
      const response = await fetch("/api/reels/sources", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ...video, analyse }),
      });
      const body = await response.json();
      if (!response.ok) {
        setError(body.error ?? "Could not save that video.");
        return;
      }
      setSaved((current) => new Set(current).add(video.video_id));
    } finally {
      setSaving("");
    }
  }

  const videos = task?.result?.videos ?? [];
  const providers = task?.result?.providers ?? [];

  return (
    <>
      <form className="reels-search" onSubmit={search}>
        <div className="field">
          <label htmlFor="niche">Niche</label>
          <select
            id="niche"
            value={niche}
            onChange={(e) => setNiche(e.target.value)}
          >
            {NICHES.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="keyword">Keyword (optional)</label>
          <input
            id="keyword"
            value={keyword}
            placeholder="drift, dashcam, transformation…"
            onChange={(e) => setKeyword(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="sort">Sort by</label>
          <select id="sort" value={sort} onChange={(e) => setSort(e.target.value)}>
            {SORTS.map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </div>
        <button className="btn-primary" type="submit" disabled={running}>
          {running ? "Searching…" : "Find viral videos"}
        </button>
      </form>

      {error && <p className="notice notice-error">{error}</p>}
      {stalled && <p className="notice notice-error">{stalled}</p>}
      {task?.status === "failed" && (
        <p className="notice notice-error">{task.error || "The search failed."}</p>
      )}

      {/* A completed search that found nothing because no provider could run
          is a configuration problem, not an empty result set. The worker says
          which in `note`; fall back to the per-provider reasons. */}
      {task?.status === "done" && videos.length === 0 && task.result?.note && (
        <p className="notice notice-error">{task.result.note}</p>
      )}
      {task?.status === "done" &&
        videos.length === 0 &&
        !task.result?.note &&
        providers.length > 0 &&
        providers.every((p) => p.availability !== "ready") && (
          <p className="notice notice-error">
            No discovery source is available.{" "}
            {providers.map((p) => `${p.platform}: ${p.reason}`).join(" · ")}
          </p>
        )}

      {providers.length > 0 && (
        <div className="reels-providers">
          {providers
            .filter((p) => p.availability !== "ready")
            .map((provider) => (
              <p key={provider.platform} className="note">
                <strong>{provider.platform}</strong>: {provider.reason}
              </p>
            ))}
        </div>
      )}

      {running && !stalled && (
        <p className="note">
          {task?.status === "running"
            ? "Searching. Results appear here."
            : "Queued — waiting for the worker to pick this up."}
        </p>
      )}

      {!running && task?.status === "done" && videos.length === 0 && (
        <div className="empty">
          <h3>Nothing came back</h3>
          <p>Try a different niche or a broader keyword.</p>
        </div>
      )}

      <div className="grid">
        {videos.map((video) => (
          <article className="vcard" key={`${video.platform}:${video.video_id}`}>
            <a
              className="vthumb"
              href={video.url}
              target="_blank"
              rel="noreferrer noopener"
            >
              {video.thumbnail_url ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={video.thumbnail_url} alt="" loading="lazy" />
              ) : (
                <span className="ph" />
              )}
              {video.viral_score !== null && (
                <span className={`score ${band(video.viral_score)}`}>
                  {video.viral_score}
                </span>
              )}
            </a>
            <div className="vbody">
              <p className="vtitle">{video.title || "Untitled"}</p>
              <p className="vmeta">
                {video.author} · {compact(video.views)} views ·{" "}
                {compact(video.followers)} followers
              </p>
              <p className="vmeta">
                Confidence: {video.score_confidence || "unknown"}
              </p>
              <div className="actions">
                <button
                  className="btn-primary"
                  type="button"
                  onClick={() => setClaiming(video)}
                >
                  Re Create
                </button>
                <button
                  className="btn-ghost"
                  type="button"
                  disabled={saving === video.video_id || saved.has(video.video_id)}
                  onClick={() => save(video, false)}
                >
                  {saved.has(video.video_id) ? "Saved" : "Save for reference"}
                </button>
              </div>
            </div>
          </article>
        ))}
      </div>

      {claiming && (
        <ClaimDialog
          video={claiming}
          onClose={() => setClaiming(null)}
          onDone={() => {
            setClaiming(null);
            router.push("/dashboard/reels/videos");
          }}
        />
      )}
    </>
  );
}

/**
 * Re Create: make a new version and save it to My Videos.
 *
 * One confirmation and one button. There is deliberately nothing to configure
 * -- the processing is fixed and legitimate (re-encode to the platform spec,
 * reframe to 9:16 around the subject, normalise loudness), and the Viral Score
 * and New Version Probability are computed as results rather than asked for as
 * settings.
 *
 * The rights confirmation stays because discovery finds other people's work.
 * It goes through the same /api/reels/assets route and the same backend gate
 * as a pasted link. Nothing here publishes anywhere.
 */
function ClaimDialog({
  video,
  onClose,
  onDone,
}: {
  video: Discovered;
  onClose: () => void;
  onDone: () => void;
}) {
  const [owns, setOwns] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [blocked, setBlocked] = useState("");

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (!owns || busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/reels/assets", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          url: video.url,
          ownsOrPermitted: true,
          title: video.title,
          thumbnailUrl: video.thumbnail_url,
          author: video.author,
        }),
      });
      const body = await response.json();
      if (!response.ok) {
        setError(body.error ?? "Could not start that.");
        return;
      }
      // A platform that will not release the media cannot be re-created. Say
      // so here rather than sending the user to My Videos to find a card that
      // never progresses.
      const asset = body.asset as { ingest_status?: string; ingest_detail?: string };
      if (asset?.ingest_status === "metadata_only") {
        setBlocked(
          asset.ingest_detail ||
            "This platform does not provide the video file, so a new version " +
              "cannot be made from it.",
        );
        return;
      }
      onDone();
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  if (blocked) {
    return (
      <div className="modal-back" role="dialog" aria-modal>
        <div className="modal">
          <h2>Cannot re-create this one</h2>
          <p className="notice notice-error">{blocked}</p>
          <p className="note">
            It has been saved to My Videos with its details and score, so you
            can still use it as reference.
          </p>
          <div className="modal-foot">
            <button className="btn-primary" type="button" onClick={onDone}>
              Go to My Videos
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="modal-back" role="dialog" aria-modal>
      <div className="modal">
        <h2>Re Create this video</h2>
        <p className="note">
          {video.title || "This video"} — {video.author || "unknown creator"} on{" "}
          {video.platform}.
        </p>
        <p className="note">
          A new version will be made and saved to My Videos. Nothing is posted
          anywhere.
        </p>
        {error && <p className="notice notice-error">{error}</p>}
        <form onSubmit={submit}>
          <label className="reels-attest">
            <input
              type="checkbox"
              checked={owns}
              onChange={(e) => setOwns(e.target.checked)}
              required
            />
            <span>I own this content or have permission to reuse it.</span>
          </label>
          <div className="modal-foot">
            <button className="btn-ghost" type="button" onClick={onClose}>
              Cancel
            </button>
            <button className="btn-primary" type="submit" disabled={!owns || busy}>
              {busy ? "Starting…" : "Re Create"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function band(score: number): string {
  if (score >= 85) return "score-exceptional";
  if (score >= 65) return "score-high";
  if (score >= 40) return "score-moderate";
  return "score-low";
}

function compact(value: number | null): string {
  if (value === null || value === undefined) return "—";
  return Intl.NumberFormat("en", { notation: "compact" }).format(value);
}
