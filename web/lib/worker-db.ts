/**
 * Database access for the off-platform worker.
 *
 * The worker acts on jobs it does not own, so it cannot use a user session,
 * and giving it a service-role key would hand it authority over every table.
 * Instead it reaches three SECURITY DEFINER functions that each verify a
 * shared secret and touch exactly one thing. A leaked publishable key is
 * useless here: without the worker token the functions refuse.
 */

import { createClient } from "@supabase/supabase-js";

function rpcClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL ?? process.env.SUPABASE_URL;
  const key =
    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? process.env.SUPABASE_KEY;
  if (!url || !key) {
    throw new Error("Supabase is not configured for this deployment.");
  }
  return createClient(url, key, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}

/** Constant-time-ish comparison of the worker's bearer token. */
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

function workerToken(): string {
  const token = process.env.WORKER_TOKEN;
  if (!token) throw new Error("WORKER_TOKEN is not set on this deployment.");
  return token;
}

export async function workerClaimJob() {
  const { data, error } = await rpcClient().rpc("worker_claim_job", {
    worker_token: workerToken(),
  });
  if (error) throw new Error(error.message);
  return (data ?? [])[0] ?? null;
}

export async function workerUpdateJob(params: {
  id: string;
  status?: string;
  stage?: string;
  progress?: number;
  message?: string;
  title?: string | null;
  error?: string | null;
}) {
  const { data, error } = await rpcClient().rpc("worker_update_job", {
    worker_token: workerToken(),
    job: params.id,
    new_status: params.status ?? null,
    new_stage: params.stage ?? null,
    new_progress: params.progress ?? null,
    new_message: params.message ?? null,
    new_title: params.title ?? null,
    new_error: params.error ?? null,
  });
  if (error) throw new Error(error.message);
  return (data ?? [])[0] ?? null;
}

export async function workerRegisterVideo(params: {
  jobId: string;
  channelSlug: string;
  videoKey: string;
  title: string;
  description?: string;
  durationSeconds?: number | null;
  width?: number | null;
  height?: number | null;
  fps?: number | null;
  videoPath: string;
  thumbnailPath?: string | null;
  reviewStatus?: string;
  reviewLog?: unknown;
}) {
  const { data, error } = await rpcClient().rpc("worker_register_video", {
    worker_token: workerToken(),
    job: params.jobId,
    p_channel_slug: params.channelSlug,
    p_video_key: params.videoKey,
    p_title: params.title,
    p_description: params.description ?? "",
    p_duration: params.durationSeconds ?? null,
    p_width: params.width ?? null,
    p_height: params.height ?? null,
    p_fps: params.fps ?? null,
    p_video_path: params.videoPath,
    p_thumbnail_path: params.thumbnailPath ?? null,
    p_review_status: params.reviewStatus ?? "",
    p_review_log: params.reviewLog ?? {},
  });
  if (error) throw new Error(error.message);
  return (data ?? [])[0] ?? null;
}
