"use client";

import { useCallback, useEffect, useState } from "react";
import type { VideoRow } from "@/lib/repositories";

/**
 * Library grid.
 *
 * Cards carry no media URL. A thumbnail is fetched as a short-lived signed URL
 * when the card mounts, and the video URL is minted only when Play is pressed,
 * so nothing in the markup is a durable link to a private object.
 */

function formatDuration(seconds: number | null): string {
  if (!seconds) return "";
  const total = Math.round(seconds);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

function statusChip(status: string) {
  const ok = /approved/i.test(status);
  const flagged = /flag/i.test(status);
  return (
    <span className={`chip ${ok ? "chip-ok" : flagged ? "chip-err" : ""}`}>
      {ok ? "Approved" : flagged ? "Flagged" : status || "Saved"}
    </span>
  );
}

function Thumb({ videoId, title }: { videoId: string; title: string }) {
  const [src, setSrc] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`/api/media/${videoId}/thumbnail`, {
          cache: "no-store",
        });
        if (!res.ok) throw new Error(String(res.status));
        const data = (await res.json()) as { url?: string };
        if (!cancelled && data.url) setSrc(data.url);
        else if (!cancelled) setFailed(true);
      } catch {
        if (!cancelled) setFailed(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [videoId]);

  if (src) return <img src={src} alt="" loading="lazy" />;
  if (failed) {
    return (
      <span className="ph" aria-hidden>
        <svg width="30" height="30" viewBox="0 0 24 24" fill="none">
          <path d="M4 5h16v14H4z" stroke="currentColor" strokeWidth="1.6" />
          <path d="m4 15 4.5-4.5L13 15l3-3 4 4" stroke="currentColor" strokeWidth="1.6" />
        </svg>
      </span>
    );
  }
  return <span className="skeleton" style={{ position: "absolute", inset: 0 }} />;
}

export function VideoGrid({ videos }: { videos: VideoRow[] }) {
  const [playing, setPlaying] = useState<{ url: string; title: string } | null>(null);
  const [loadingId, setLoadingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const play = useCallback(async (video: VideoRow) => {
    setLoadingId(video.id);
    setError(null);
    try {
      const res = await fetch(`/api/media/${video.id}/video`, { cache: "no-store" });
      const data = (await res.json()) as { url?: string; error?: string };
      if (!res.ok || !data.url) {
        setError(data.error ?? "Could not open that video.");
        return;
      }
      setPlaying({ url: data.url, title: video.title });
    } catch {
      setError("Could not reach the server.");
    } finally {
      setLoadingId(null);
    }
  }, []);

  // Escape closes the player.
  useEffect(() => {
    if (!playing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPlaying(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [playing]);

  return (
    <>
      {error && <div className="notice notice-error">{error}</div>}

      <div className="grid">
        {videos.map((video) => (
          <article className="vcard" key={video.id}>
            <div className="vthumb">
              <Thumb videoId={video.id} title={video.title} />
            </div>
            <div className="vbody">
              <h3 className="vtitle" dir="auto">
                {video.title || "Untitled"}
              </h3>
              <div className="vmeta">
                <span>{new Date(video.created_at).toLocaleDateString()}</span>
                {video.duration_seconds ? <span>· {formatDuration(video.duration_seconds)}</span> : null}
                {video.width && video.height ? <span>· {video.width}×{video.height}</span> : null}
              </div>
              <div style={{ marginTop: 9 }}>{statusChip(video.review_status)}</div>
              <button
                className="btn-play"
                type="button"
                onClick={() => void play(video)}
                disabled={loadingId === video.id}
              >
                {loadingId === video.id ? "Opening…" : "▶  Play"}
              </button>
            </div>
          </article>
        ))}
      </div>

      {playing && (
        <div className="modal-back" onClick={() => setPlaying(null)} role="presentation">
          <div className="modal" onClick={(e) => e.stopPropagation()}>
            <video src={playing.url} controls autoPlay playsInline />
            <div className="modal-foot">
              <span style={{ fontSize: 13.5 }} dir="auto">
                {playing.title}
              </span>
              <button className="btn-ghost" style={{ width: "auto" }} type="button" onClick={() => setPlaying(null)}>
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
