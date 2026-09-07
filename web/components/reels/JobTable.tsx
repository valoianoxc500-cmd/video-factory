"use client";

import { useState } from "react";
import { PLATFORMS, type PublishJobRow } from "@/lib/vrf";

/** Publish jobs, with what each one is waiting for and why. */

export function JobTable({
  initial,
  emptyTitle,
  emptyBody,
  cancellable = true,
}: {
  initial: PublishJobRow[];
  emptyTitle: string;
  emptyBody: string;
  cancellable?: boolean;
}) {
  const [jobs, setJobs] = useState(initial);
  const [error, setError] = useState("");

  async function cancel(id: string) {
    const response = await fetch(`/api/reels/jobs/${id}`, { method: "DELETE" });
    const body = await response.json();
    if (!response.ok) {
      setError(body.error ?? "Could not cancel that job.");
      return;
    }
    setJobs((current) =>
      current.map((job) => (job.id === id ? (body.job as PublishJobRow) : job)),
    );
  }

  if (jobs.length === 0) {
    return (
      <div className="empty">
        <h3>{emptyTitle}</h3>
        <p>{emptyBody}</p>
      </div>
    );
  }

  return (
    <>
      {error && <p className="notice notice-error">{error}</p>}
      <div className="reels-jobs">
        {jobs.map((job) => (
          <div className="jrow" key={job.id}>
            <div className="jtopic">
              <strong>{label(job.platform)}</strong>
              <small>{describe(job)}</small>
            </div>
            <span className={`chip ${chip(job.status)}`}>{job.status}</span>
            {job.post_url && (
              <a
                className="btn-ghost"
                href={job.post_url}
                target="_blank"
                rel="noreferrer noopener"
              >
                View post
              </a>
            )}
            {cancellable && (job.status === "queued" || job.status === "processing") && (
              <button className="btn-ghost" type="button" onClick={() => cancel(job.id)}>
                Cancel
              </button>
            )}
          </div>
        ))}
      </div>
    </>
  );
}

function describe(job: PublishJobRow): string {
  if (job.status === "unsupported") return job.error;
  if (job.status === "published") {
    return job.error || `Posted ${when(job.updated_at)}`;
  }
  if (job.status === "failed") {
    return job.error || "Failed.";
  }
  if (job.scheduled_for) {
    const mode =
      job.mode === "native_schedule"
        ? "the platform will publish it"
        : "held here until then";
    return `Scheduled for ${when(job.scheduled_for)} — ${mode}`;
  }
  if (job.attempts > 0 && job.next_attempt_at) {
    return `Attempt ${job.attempts} failed; retrying ${when(job.next_attempt_at)}`;
  }
  return "Waiting for the worker";
}

function chip(status: string): string {
  if (status === "published") return "chip-ok";
  if (status === "failed" || status === "unsupported") return "chip-err";
  return "chip-run";
}

function label(platform: string): string {
  return PLATFORMS.find((p) => p.platform === platform)?.label ?? platform;
}

function when(value: string | null): string {
  if (!value) return "";
  return new Date(value).toLocaleString();
}
