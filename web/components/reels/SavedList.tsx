"use client";

import { useState } from "react";
import type { SourceRow } from "@/lib/vrf";

/**
 * Saved discoveries, with whatever analysis has come back.
 *
 * The analysis of a discovered video is read from its public metadata and
 * thumbnail; the video is never copied. That is stated on the card rather than
 * left for the user to assume.
 */

export function SavedList({ initial }: { initial: SourceRow[] }) {
  const [sources, setSources] = useState(initial);
  const [open, setOpen] = useState<string>("");
  const [error, setError] = useState("");

  async function remove(id: string) {
    const response = await fetch(`/api/reels/sources/${id}`, { method: "DELETE" });
    if (!response.ok) {
      setError("Could not remove that video.");
      return;
    }
    setSources((current) => current.filter((s) => s.id !== id));
  }

  if (sources.length === 0) {
    return (
      <div className="empty">
        <h3>Nothing saved yet</h3>
        <p>Save a video from Discover to keep its numbers and analysis here.</p>
      </div>
    );
  }

  return (
    <>
      {error && <p className="notice notice-error">{error}</p>}
      <div className="grid">
        {sources.map((source) => {
          const analysis = source.analysis as Record<string, unknown>;
          const analysed = Boolean(analysis?.hook || analysis?.why_it_performs);
          return (
            <article className="vcard" key={source.id}>
              <a
                className="vthumb"
                href={source.url}
                target="_blank"
                rel="noreferrer noopener"
              >
                {source.thumbnail_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={source.thumbnail_url} alt="" loading="lazy" />
                ) : (
                  <span className="ph" />
                )}
                {source.viral_score !== null && (
                  <span className="score">{source.viral_score}</span>
                )}
              </a>
              <div className="vbody">
                <p className="vtitle">{source.title || "Untitled"}</p>
                <p className="vmeta">
                  {source.author} · {compact(source.views)} views
                </p>

                {analysed ? (
                  <>
                    <p className="reels-hook">
                      <strong>Hook:</strong> {String(analysis.hook ?? "")}
                    </p>
                    {open === source.id ? (
                      <div className="reels-analysis">
                        <Line label="Topic" value={analysis.topic} />
                        <Line label="Format" value={analysis.format} />
                        <Line label="Pacing" value={analysis.pacing} />
                        <Line label="Captions" value={analysis.caption_style} />
                        <Line label="Audio" value={analysis.audio_style} />
                        <List
                          label="Why it performs"
                          value={analysis.why_it_performs}
                        />
                        <List
                          label="What you could reuse"
                          value={analysis.replicable_elements}
                        />
                        {Array.isArray(analysis.notes) &&
                          (analysis.notes as string[]).map((note) => (
                            <p className="note" key={note}>
                              {note}
                            </p>
                          ))}
                      </div>
                    ) : null}
                    <button
                      className="btn-ghost"
                      type="button"
                      onClick={() => setOpen(open === source.id ? "" : source.id)}
                    >
                      {open === source.id ? "Hide analysis" : "Show analysis"}
                    </button>
                  </>
                ) : (
                  <p className="note">Not analysed yet.</p>
                )}

                <div className="actions">
                  <button
                    className="btn-ghost"
                    type="button"
                    onClick={() => remove(source.id)}
                  >
                    Remove
                  </button>
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </>
  );
}

function Line({ label, value }: { label: string; value: unknown }) {
  if (!value) return null;
  return (
    <p className="vmeta">
      <strong>{label}:</strong> {String(value)}
    </p>
  );
}

function List({ label, value }: { label: string; value: unknown }) {
  if (Array.isArray(value)) {
    if (value.length === 0) return null;
    return (
      <div className="vmeta">
        <strong>{label}:</strong>
        <ul>
          {value.map((item) => (
            <li key={String(item)}>{String(item)}</li>
          ))}
        </ul>
      </div>
    );
  }
  return <Line label={label} value={value} />;
}

function compact(value: number | null): string {
  if (value === null || value === undefined) return "—";
  return Intl.NumberFormat("en", { notation: "compact" }).format(value);
}
