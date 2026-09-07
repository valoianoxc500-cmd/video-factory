"use client";

import { useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { PLATFORMS, type AssetRow } from "@/lib/vrf";
import {
  INGEST_PLATFORMS,
  SUPPORTED_INGEST,
  detectPlatform,
  type IngestPlatform,
} from "@/lib/vrf-ingest";

/**
 * My Videos: paste a link, confirm you may use it, and the pipeline runs.
 *
 * The form asks two things because only two things are genuinely the user's to
 * answer. Platform, video id and whether the file can be fetched are derived
 * from the URL server-side -- asking a person to classify their own link is
 * work the software should do.
 *
 * The rights question survives the simplification. It is one checkbox now
 * rather than a radio group, but it is still required, still recorded against
 * the asset, and still the thing the backend refuses to publish without.
 */

interface Account {
  platform: string;
  connected: boolean;
}

/**
 * What the user is waiting for, in their terms.
 *
 * These are ingest states, but "importing" is not what someone who pressed
 * Re Create is waiting for -- they are waiting for a new video. The labels
 * describe the job, not the plumbing.
 */
const INGEST_LABEL: Record<string, string> = {
  pending: "Queued",
  fetching: "Fetching the source",
  imported: "Making the new version",
  metadata_only: "Cannot re-create",
  failed: "Failed",
};

const PUBLISH_LABEL: Record<string, string> = {
  none: "Not published",
  queued: "Queued",
  scheduled: "Scheduled",
  published: "Published",
  failed: "Publish failed",
};

/** Rough progress for the bar; the label carries the real detail. */
const INGEST_PROGRESS: Record<string, number> = {
  pending: 10,
  fetching: 45,
  imported: 70,
  metadata_only: 100,
  failed: 100,
};

export function MyVideos({
  initial,
  accounts,
}: {
  initial: AssetRow[];
  accounts: Account[];
}) {
  const router = useRouter();
  const [assets, setAssets] = useState(initial);
  const [adding, setAdding] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [url, setUrl] = useState("");
  const [owns, setOwns] = useState(false);
  const [publishing, setPublishing] = useState<AssetRow | null>(null);

  const connected = useMemo(
    () => new Set(accounts.filter((a) => a.connected).map((a) => a.platform)),
    [accounts],
  );

  // Detected as the user types, so the consequence of the link is visible
  // before they commit to it rather than after a round trip.
  const detected = url.trim() ? detectPlatform(url) : null;

  // Poll while any card is still working towards a new version -- which
  // includes an imported file whose re-encode has not finished, not just the
  // fetch itself.
  const settled = assets.every(
    (a) =>
      Boolean(a.processed_path) ||
      a.ingest_status === "metadata_only" ||
      a.ingest_status === "failed",
  );
  useEffect(() => {
    if (settled) return;
    const timer = setInterval(async () => {
      try {
        const res = await fetch("/api/reels/assets", { cache: "no-store" });
        if (!res.ok) return;
        const body = (await res.json()) as { assets?: AssetRow[] };
        if (body.assets) setAssets(body.assets);
      } catch {
        /* transient */
      }
    }, 6000);
    return () => clearInterval(timer);
  }, [settled]);

  async function add(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const response = await fetch("/api/reels/assets", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ url: url.trim(), ownsOrPermitted: owns }),
      });
      const body = await response.json();
      if (!response.ok) {
        setError(body.error ?? "Could not add that video.");
        return;
      }
      setAssets((current) => [body.asset as AssetRow, ...current]);
      setAdding(false);
      setUrl("");
      setOwns(false);
      router.refresh();
    } catch {
      setError("Could not reach the server.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {error && <p className="notice notice-error">{error}</p>}

      <div className="sec-head">
        <h2>Your videos</h2>
        <button
          className="btn-primary"
          type="button"
          onClick={() => setAdding((v) => !v)}
        >
          {adding ? "Cancel" : "Add video"}
        </button>
      </div>

      {adding && (
        <form className="reels-form" onSubmit={add}>
          <div className="field">
            <label htmlFor="url">Video URL</label>
            <input
              id="url"
              name="url"
              required
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://www.tiktok.com/@you/video/…"
              autoComplete="off"
            />
            <p className="note">
              {detected ? (
                <>
                  Detected <strong>{INGEST_PLATFORMS[detected].label}</strong>.{" "}
                  {INGEST_PLATFORMS[detected].mediaAccess ===
                  "owner_connected_account"
                    ? connected.has(detected)
                      ? "Your connected account will be used to import it."
                      : "Connect that account first to import the file."
                    : INGEST_PLATFORMS[detected].limitation}
                </>
              ) : (
                <>
                  Supported:{" "}
                  {SUPPORTED_INGEST.map((p) => p.label).join(", ")}.
                </>
              )}
            </p>
          </div>

          <label className="reels-attest">
            <input
              type="checkbox"
              checked={owns}
              onChange={(e) => setOwns(e.target.checked)}
              required
            />
            <span>I own this content or have permission to reuse it.</span>
          </label>

          <button className="btn-primary" type="submit" disabled={busy || !owns}>
            {busy ? "Adding…" : "Add & Process"}
          </button>
        </form>
      )}

      {assets.length === 0 ? (
        <div className="empty">
          <h3>No videos yet</h3>
          <p>
            Paste a link to a video you own or have permission to reuse, and it
            will be analysed and prepared for posting.
          </p>
        </div>
      ) : (
        <div className="grid">
          {assets.map((asset) => (
            <AssetCard
              key={asset.id}
              asset={asset}
              onPublish={() => setPublishing(asset)}
            />
          ))}
        </div>
      )}

      {publishing && (
        <PublishDialog
          asset={publishing}
          connected={connected}
          onClose={() => setPublishing(null)}
          onDone={() => {
            setPublishing(null);
            router.push("/dashboard/reels/queue");
          }}
        />
      )}
    </>
  );
}

function AssetCard({
  asset,
  onPublish,
}: {
  asset: AssetRow;
  onPublish: () => void;
}) {
  const platform =
    INGEST_PLATFORMS[asset.source_platform as IngestPlatform]?.label ??
    asset.source_platform ??
    "";
  const blocked =
    asset.ingest_status === "metadata_only" || asset.ingest_status === "failed";
  const ready = Boolean(asset.processed_path);
  // Once the file is in, the remaining wait is the re-encode. "Imported" is a
  // step the user never asked about; "New version ready" is the thing they did.
  const ingestLabel = ready
    ? "New version ready"
    : INGEST_LABEL[asset.ingest_status] ?? asset.ingest_status;
  const progress = ready ? 100 : INGEST_PROGRESS[asset.ingest_status] ?? 0;

  return (
    <article className="vcard">
      <div className="vthumb">
        {asset.thumbnail_url ? (
          // eslint-disable-next-line @next/next/no-img-element
          <img src={asset.thumbnail_url} alt="" loading="lazy" />
        ) : (
          <span className="ph" aria-hidden />
        )}
        {asset.original_viral_score !== null && (
          <span className={`score ${band(asset.original_viral_score)}`}>
            {asset.original_viral_score}
          </span>
        )}
      </div>

      <div className="vbody">
        <p className="vtitle">{asset.title || "Untitled"}</p>
        <p className="vmeta">
          {platform}
          {asset.source_author ? ` · ${asset.source_author}` : ""}
        </p>

        <div className="reels-stats">
          <span>
            Original score:{" "}
            <strong>
              {asset.original_viral_score ?? "—"}
            </strong>
          </span>
          <span>
            New version:{" "}
            <strong>
              {asset.new_version_probability !== null
                ? `${asset.new_version_probability}%`
                : "—"}
            </strong>
            {asset.new_version_probability !== null && (
              <span className="note">
                {" "}
                ({asset.probability_confidence || "low"} confidence — an
                estimate, not a guarantee)
              </span>
            )}
          </span>
        </div>

        <p className="vmeta">
          <span className={`chip ${blocked ? "chip-err" : ready ? "chip-ok" : "chip-run"}`}>
            {ingestLabel}
          </span>{" "}
          <span className="chip">
            {PUBLISH_LABEL[asset.publish_state] ?? asset.publish_state}
          </span>
        </p>

        {!blocked && !ready && (
          <div className="bar">
            <span style={{ width: `${Math.max(4, progress)}%` }} />
          </div>
        )}

        {asset.ingest_detail && blocked && (
          <p className="note">{asset.ingest_detail}</p>
        )}

        <div className="actions">
          <button
            className="btn-ghost"
            type="button"
            disabled={!ready}
            onClick={onPublish}
          >
            Publish or schedule
          </button>
          {asset.source_url && (
            <a
              className="btn-ghost"
              href={asset.source_url}
              target="_blank"
              rel="noreferrer noopener"
            >
              Open original
            </a>
          )}
        </div>
      </div>
    </article>
  );
}

function band(score: number): string {
  if (score >= 80) return "score-high";
  if (score >= 55) return "score-mid";
  return "score-low";
}

function PublishDialog({
  asset,
  connected,
  onClose,
  onDone,
}: {
  asset: AssetRow;
  connected: Set<string>;
  onClose: () => void;
  onDone: () => void;
}) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const form = new FormData(event.currentTarget);
    const platforms = form.getAll("platforms").map(String);
    const when = String(form.get("scheduledFor") ?? "");

    try {
      const response = await fetch("/api/reels/publish", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          assetId: asset.id,
          caption: form.get("caption"),
          platforms,
          scheduledFor: when ? new Date(when).toISOString() : null,
          attribution: form.get("attribution"),
        }),
      });
      const body = await response.json();
      if (!response.ok) {
        setError(body.error ?? "Could not queue that.");
        return;
      }
      onDone();
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="modal-back" role="dialog" aria-modal>
      <div className="modal">
        <h2>Publish “{asset.title}”</h2>
        {error && <p className="notice notice-error">{error}</p>}
        <form onSubmit={submit}>
          <div className="field">
            <label htmlFor="caption">Caption</label>
            <input id="caption" name="caption" maxLength={2200} />
          </div>

          {asset.needs_attribution && (
            <div className="field">
              <label htmlFor="attribution">Credit line (required)</label>
              <input
                id="attribution"
                name="attribution"
                defaultValue={
                  asset.rights_holder ? `Credit: ${asset.rights_holder}` : ""
                }
                required
              />
            </div>
          )}

          <fieldset className="reels-rights">
            <legend>Where to post</legend>
            {PLATFORMS.map((platform) => {
              const unavailable = !platform.supported;
              const notConnected =
                platform.supported && !connected.has(platform.platform);
              return (
                <label className="reels-radio" key={platform.platform}>
                  <input
                    type="checkbox"
                    name="platforms"
                    value={platform.platform}
                    disabled={unavailable || notConnected}
                  />
                  {platform.label}
                  {unavailable && <span className="note"> — {platform.note}</span>}
                  {notConnected && <span className="note"> — not connected</span>}
                  {platform.supported && !platform.nativeScheduling && (
                    <span className="note"> — scheduled posts are held here</span>
                  )}
                </label>
              );
            })}
          </fieldset>

          <div className="field">
            <label htmlFor="scheduledFor">Schedule for (optional)</label>
            <input id="scheduledFor" name="scheduledFor" type="datetime-local" />
          </div>

          <div className="modal-foot">
            <button className="btn-ghost" type="button" onClick={onClose}>
              Cancel
            </button>
            <button className="btn-primary" type="submit" disabled={busy}>
              {busy ? "Queueing…" : "Queue it"}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
