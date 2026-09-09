"use client";

import { useEffect, useState } from "react";
import type { JobRow } from "@/lib/repositories";
import { customerProgressState, customerSafeError } from "@/lib/customer-errors";

function label(job: JobRow): string {
  return customerProgressState(job.status, job.stage);
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
              {job.status === "error" && job.error ? ` · ${customerSafeError(job.error)}` : ""}
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
