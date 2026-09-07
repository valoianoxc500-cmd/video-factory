/**
 * Job store backed by Postgres (Supabase).
 *
 * This replaced a Vercel Blob document store. Blob had no compare-and-set, so
 * the whole queue lived in one JSON document that every write re-read and
 * rewrote, and reads had to race a CDN cache against a `list()` call that went
 * from 0.2s to 100s+ during a Blob incident. It finally failed outright when
 * the store hit its plan quota and was suspended: job creation and reads both
 * returned 503, taking the site down.
 *
 * Postgres removes that class of failure. Each job is a row, so writes touch
 * one record instead of rewriting the queue, reads are always current, and two
 * workers can no longer claim the same job -- the claim is a conditional
 * UPDATE that only one of them can win.
 *
 * Rendered MP4s and thumbnails live in the `media` storage bucket of the same
 * project; the worker uploads them and stores the public URL on the row.
 */

import { DEFAULT_ENGINE } from "./engines";

export type JobStatus = "queued" | "running" | "done" | "error";

export interface Job {
  id: string;
  topic: string;
  /** Which generation engine runs this job; see lib/engines.ts. */
  engine: string;
  /** Sub-mode within the engine (Horror story type). Empty when unused. */
  style: string;
  /** Script language for narration and captions. Empty = channel default. */
  language: string;
  status: JobStatus;
  progress: number;
  stage: string;
  message: string;
  createdAt: string;
  updatedAt: string;
  title: string | null;
  videoUrl: string | null;
  thumbnailUrl: string | null;
  durationSeconds: number | null;
  error: string | null;
}

const MAX_JOBS = 60;
const TABLE = "jobs";
/** A job the worker stopped reporting on is presumed dead after this. */
const STALE_RUNNING_MS = 30 * 60 * 1000;

/** Storage failure the caller should report verbatim rather than swallow. */
export class JobStoreError extends Error {
  readonly status: number;
  constructor(message: string, status = 503) {
    super(message);
    this.name = "JobStoreError";
    this.status = status;
  }
}

function config(): { url: string; key: string } {
  const url = process.env.SUPABASE_URL;
  const key = process.env.SUPABASE_KEY;
  if (!url || !key) {
    throw new JobStoreError(
      "Job storage is not configured: SUPABASE_URL and SUPABASE_KEY must be " +
        "set on the Vercel project.",
      500,
    );
  }
  return { url: url.replace(/\/$/, ""), key };
}

/** Row shape in Postgres. snake_case there, camelCase in the API. */
interface Row {
  id: string;
  topic: string;
  engine: string | null;
  style: string | null;
  language: string | null;
  status: JobStatus;
  progress: number;
  stage: string;
  message: string;
  title: string | null;
  video_url: string | null;
  thumbnail_url: string | null;
  duration_seconds: number | null;
  error: string | null;
  created_at: string;
  updated_at: string;
}

function toJob(row: Row): Job {
  return {
    id: row.id,
    topic: row.topic,
    // Rows written before the engine column existed read as Football News.
    engine: row.engine ?? DEFAULT_ENGINE,
    style: row.style ?? "",
    language: row.language ?? "",
    status: row.status,
    progress: row.progress ?? 0,
    stage: row.stage ?? "",
    message: row.message ?? "",
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    title: row.title,
    videoUrl: row.video_url,
    thumbnailUrl: row.thumbnail_url,
    durationSeconds: row.duration_seconds,
    error: row.error,
  };
}

function toRow(job: Job): Record<string, unknown> {
  return {
    id: job.id,
    topic: job.topic,
    engine: job.engine,
    style: job.style,
    language: job.language,
    status: job.status,
    progress: job.progress,
    stage: job.stage,
    message: job.message,
    title: job.title,
    video_url: job.videoUrl,
    thumbnail_url: job.thumbnailUrl,
    duration_seconds: job.durationSeconds,
    error: job.error,
    updated_at: new Date().toISOString(),
  };
}

async function rest(
  path: string,
  init: RequestInit & { timeoutMs?: number } = {},
): Promise<Response> {
  const { url, key } = config();
  const { timeoutMs = 8000, headers, ...rest } = init;
  let res: Response;
  try {
    res = await fetch(`${url}/rest/v1/${path}`, {
      ...rest,
      cache: "no-store",
      signal: AbortSignal.timeout(timeoutMs),
      headers: {
        apikey: key,
        authorization: `Bearer ${key}`,
        "content-type": "application/json",
        ...headers,
      },
    });
  } catch (err) {
    throw describeStoreError(err);
  }
  if (!res.ok) {
    throw describeStoreError(new Error(`${res.status} ${await res.text()}`));
  }
  return res;
}

export function newJob(
  topic: string,
  engine: string = DEFAULT_ENGINE,
  style: string = "",
  language: string = "",
): Job {
  const now = new Date().toISOString();
  return {
    id: crypto.randomUUID(),
    topic,
    engine,
    style,
    language,
    status: "queued",
    progress: 0,
    stage: "queued",
    message: "Waiting for a worker to pick this up",
    createdAt: now,
    updatedAt: now,
    title: null,
    videoUrl: null,
    thumbnailUrl: null,
    durationSeconds: null,
    error: null,
  };
}

/** Read every job, newest first. Returns [] before the first write. */
export async function listJobs(limit = MAX_JOBS): Promise<Job[]> {
  const res = await rest(
    `${TABLE}?select=*&order=created_at.desc&limit=${limit}`,
  );
  const rows = (await res.json()) as Row[];
  return Array.isArray(rows) ? rows.map(toJob) : [];
}

export async function getJob(id: string): Promise<Job | null> {
  const res = await rest(`${TABLE}?select=*&id=eq.${encodeURIComponent(id)}`);
  const rows = (await res.json()) as Row[];
  return rows.length > 0 ? toJob(rows[0]) : null;
}

/** Insert a brand-new job. */
export async function createJob(job: Job): Promise<Job> {
  const res = await rest(TABLE, {
    method: "POST",
    headers: { prefer: "return=representation" },
    body: JSON.stringify({ ...toRow(job), created_at: job.createdAt }),
  });
  const rows = (await res.json()) as Row[];
  return rows.length > 0 ? toJob(rows[0]) : job;
}

/**
 * Update an existing job.
 *
 * A plain UPDATE rather than an upsert. An upsert is an INSERT that falls back
 * on conflict, so Postgres evaluates the INSERT policy -- which constrains new
 * rows to queued work with no result attached. Every progress write carries
 * status "running", so upserting failed that check and returned 500 on every
 * update the worker posted. Callers only ever save a job they just read.
 */
export async function saveJob(job: Job): Promise<Job> {
  const res = await rest(`${TABLE}?id=eq.${encodeURIComponent(job.id)}`, {
    method: "PATCH",
    headers: { prefer: "return=representation" },
    body: JSON.stringify(toRow(job)),
  });
  const rows = (await res.json()) as Row[];
  return rows.length > 0 ? toJob(rows[0]) : job;
}

/**
 * Claim the oldest queued job for the worker.
 *
 * The status predicate is part of the UPDATE, so if two workers race, the
 * second matches zero rows and gets null rather than a duplicate render. The
 * Blob store could not express this.
 */
export async function claimNextJob(): Promise<Job | null> {
  const queued = await rest(
    `${TABLE}?select=id&status=eq.queued&order=created_at.asc&limit=1`,
  );
  const candidates = (await queued.json()) as { id: string }[];
  if (candidates.length === 0) return null;

  const res = await rest(
    `${TABLE}?id=eq.${candidates[0].id}&status=eq.queued`,
    {
      method: "PATCH",
      headers: { prefer: "return=representation" },
      body: JSON.stringify({
        status: "running",
        stage: "claimed",
        message: "Worker picked up this job",
        updated_at: new Date().toISOString(),
      }),
    },
  );
  const rows = (await res.json()) as Row[];
  return rows.length > 0 ? toJob(rows[0]) : null;
}

/** Fail jobs whose worker stopped reporting, so the queue cannot wedge. */
export async function expireStaleJobs(): Promise<void> {
  const cutoff = new Date(Date.now() - STALE_RUNNING_MS).toISOString();
  try {
    await rest(`${TABLE}?status=eq.running&updated_at=lt.${cutoff}`, {
      method: "PATCH",
      body: JSON.stringify({
        status: "error",
        error: "The worker stopped reporting progress on this job.",
        message: "Abandoned",
        updated_at: new Date().toISOString(),
      }),
    });
  } catch {
    /* housekeeping only; never fail the caller */
  }
}

/**
 * Translate a raw storage failure into something actionable.
 */
export function describeStoreError(err: unknown): JobStoreError {
  if (err instanceof JobStoreError) return err;
  const message = err instanceof Error ? err.message : String(err);
  if (/timed out|abort/i.test(message)) {
    return new JobStoreError(`Job storage timed out: ${message}`, 504);
  }
  return new JobStoreError(`Job storage unavailable: ${message}`, 503);
}

export function isWorkerAuthorized(request: Request): boolean {
  const expected = process.env.WORKER_TOKEN;
  if (!expected) return false;
  const header = request.headers.get("authorization") ?? "";
  const supplied = header.startsWith("Bearer ") ? header.slice(7) : "";
  if (supplied.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) {
    diff |= expected.charCodeAt(i) ^ supplied.charCodeAt(i);
  }
  return diff === 0;
}

