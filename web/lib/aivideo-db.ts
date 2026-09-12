/**
 * Database access for the AI Video Maker worker.
 *
 * Same shape as `worker-db.ts` and for the same reason: the worker acts on
 * rows it does not own, so it cannot use a user session, and handing it a
 * service-role key would give it authority over every table. Instead it
 * reaches two SECURITY DEFINER functions that each verify the shared worker
 * token and touch exactly one table.
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

function workerToken(): string {
  const token = process.env.WORKER_TOKEN;
  if (!token) throw new Error("WORKER_TOKEN is not configured.");
  return token;
}

export type ClaimedJob = {
  id: string;
  topic: string;
  spec: Record<string, unknown>;
  attempts: number;
};

export async function aiVideoClaimJob(): Promise<ClaimedJob | null> {
  const { data, error } = await rpcClient().rpc("ai_video_claim_job", {
    p_token: workerToken(),
  });
  if (error) throw new Error(error.message);
  const row = Array.isArray(data) ? data[0] : data;
  if (!row) return null;
  return {
    id: row.id,
    topic: row.topic ?? "",
    spec: row.spec ?? {},
    attempts: row.attempts ?? 1,
  };
}

type UpdateFields = {
  status?: string;
  stage?: string;
  progress?: number;
  message?: string;
  error?: string;
  video_url?: string;
  thumbnail_url?: string;
  duration_actual?: number;
  cost_usd?: number;
  providers_used?: string[];
  fallbacks?: string[];
};

export async function aiVideoUpdateJob(id: string, fields: UpdateFields): Promise<void> {
  // Undefined is passed through as null, which the function reads as "leave
  // this column alone" — so a progress ping cannot erase a finished result.
  const { error } = await rpcClient().rpc("ai_video_update_job", {
    p_token: workerToken(),
    p_id: id,
    p_status: fields.status ?? null,
    p_stage: fields.stage ?? null,
    p_progress: fields.progress ?? null,
    p_message: fields.message ?? null,
    p_error: fields.error ?? null,
    p_video_url: fields.video_url ?? null,
    p_thumbnail_url: fields.thumbnail_url ?? null,
    p_duration: fields.duration_actual ?? null,
    p_cost_usd: fields.cost_usd ?? null,
    p_providers: fields.providers_used ?? null,
    p_fallbacks: fields.fallbacks ?? null,
  });
  if (error) throw new Error(error.message);
}
