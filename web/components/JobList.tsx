"use client";

import { useEffect, useState } from "react";
import type { JobRow } from "@/lib/repositories";

/**
 * The caller's generation activity.
 *
 * Stage names come from the pipeline itself; this maps them to language a user
 * can act on. An unrecognised stage falls back to the raw value rather than
 * being hidden, so a pipeline change shows up rather than silently reading as
 * "Queued".
 */
const STAGE_LABEL: Record<string, string> = {
  queued: "Queued",
  claimed: "Starting",
  planning: "Researching",
  script: "Writing the script",
  image_source: "Finding visuals",
  audio_source: "Recording narration",
  process: "Preparing images",
  render_sections: "Rendering",
  assemble: "Assembling",
  thumbnail: "Making the thumbnail",
  final_review: "Reviewing",
};

function label(job: JobRow): string {
  if (job.status === "done") return "Completed";
  if (job.status === "error") return "Failed";
  return STAGE_LABEL[job.stage] ?? job.stage ?? "Queued";
}

function chipClass(job: JobRow): string {
  if (job.status === "done") return "chip chip-ok";
  if (job.status === "error") return "chip chip-err";
  return "chip chip-run";
}

export function JobList({ initial }: { initial: JobRow[] }) {
  const [jobs, setJobs] = useState(initial);

  // Poll only while something is in flight, so an idle tab is silent.
  useEffect(() => {
    const active = jobs.some((j) => j.status === "queued" || j.status === "running");
    if (!active) return;
    const timer = setInterval(async () => {
      try {
        const res = await fetch("/api/jobs", { cache: "no-store" });
        if (!res.ok) return;
        const data = (await res.json()) as { jobs?: JobRow[] };
        setJobs(data.jobs ?? []);
      } catch {
        /* transient; the next tick retries */
      }
    }, 5000);
    return () => clearInterval(timer);
  }, [jobs]);

  if (jobs.length === 0) {
    return (
      <div className="empty">
        <h3>No activity yet</h3>
        <p>Generations you start will appear here with their live progress.</p>
      </div>
    );
  }

  return (
    <div>
      {jobs.map((job) => (
        <div className="jrow" key={job.id}>
          <span className={chipClass(job)}>{label(job)}</span>
          <div className="jtopic">
            <span dir="auto">{job.title || job.topic}</span>
            <small>
              {new Date(job.created_at).toLocaleString()}
              {job.channel_slug ? ` · ${job.channel_slug.replace(/_/g, " ")}` : ""}
              {job.status === "error" && job.error ? ` · ${job.error}` : ""}
            </small>
          </div>
          {job.status === "running" || job.status === "queued" ? (
            <>
              <div className="bar">
                <span style={{ width: `${Math.max(2, job.progress)}%` }} />
              </div>
              <span style={{ fontSize: 12, color: "var(--sa-dim)", width: 34, textAlign: "right" }}>
                {job.progress}%
              </span>
            </>
          ) : null}
        </div>
      ))}
    </div>
  );
}
