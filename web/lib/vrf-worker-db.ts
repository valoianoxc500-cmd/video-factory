/**
 * Database access for the Viral Reels Finder worker.
 *
 * Same design as `lib/worker-db.ts`: the worker holds no service-role key and
 * no user session. It reaches SECURITY DEFINER functions that each verify the
 * shared worker token and touch exactly one thing. Every function carries the
 * owning `user_id` through, which is what keeps one user's token from being
 * spent on another user's job.
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
  if (!token) throw new Error("WORKER_TOKEN is not set on this deployment.");
  return token;
}

async function rpc(name: string, params: Record<string, unknown>) {
  const { data, error } = await rpcClient().rpc(name, {
    worker_token: workerToken(),
    ...params,
  });
  if (error) throw new Error(error.message);
  return data;
}

export async function vrfClaimTask() {
  return ((await rpc("vrf_worker_claim_task", {})) ?? [])[0] ?? null;
}

export async function vrfCompleteTask(params: {
  id: string;
  status: "done" | "failed" | "queued";
  result?: unknown;
  error?: string;
}) {
  return rpc("vrf_worker_complete_task", {
    task: params.id,
    new_status: params.status,
    new_result: params.result ?? {},
    new_error: params.error ?? "",
  });
}

export async function vrfClaimPublishJob() {
  return ((await rpc("vrf_worker_claim_publish_job", {})) ?? [])[0] ?? null;
}

export async function vrfUpdatePublishJob(params: {
  id: string;
  status: string;
  attempts?: number | null;
  nextAttemptAt?: string | null;
  postId?: string | null;
  postUrl?: string | null;
  error?: string | null;
}) {
  return rpc("vrf_worker_update_publish_job", {
    job: params.id,
    new_status: params.status,
    new_attempts: params.attempts ?? null,
    new_next_attempt_at: params.nextAttemptAt ?? null,
    new_post_id: params.postId ?? null,
    new_post_url: params.postUrl ?? null,
    new_error: params.error ?? null,
  });
}

/** The encrypted tokens for one user's account. Ciphertext only. */
export async function vrfAccount(userId: string, platform: string) {
  return (
    ((await rpc("vrf_worker_account", { p_user: userId, p_platform: platform })) ??
      [])[0] ?? null
  );
}

export async function vrfStoreTokens(params: {
  accountId: string;
  accessTokenEncrypted: string;
  refreshTokenEncrypted?: string | null;
  expiresAt?: string | null;
}) {
  return rpc("vrf_worker_store_tokens", {
    account: params.accountId,
    new_access: params.accessTokenEncrypted,
    new_refresh: params.refreshTokenEncrypted ?? null,
    new_expires_at: params.expiresAt ?? null,
  });
}

export async function vrfSource(id: string) {
  return ((await rpc("vrf_worker_source", { p_source: id })) ?? [])[0] ?? null;
}

export async function vrfSaveAnalysis(id: string, analysis: unknown) {
  return rpc("vrf_worker_save_analysis", { p_source: id, p_analysis: analysis });
}

export async function vrfAsset(id: string) {
  return ((await rpc("vrf_worker_asset", { p_asset: id })) ?? [])[0] ?? null;
}

export async function vrfUpdateAssetIngest(params: {
  assetId: string;
  status: string;
  detail?: string | null;
  storagePath?: string | null;
}) {
  return rpc("vrf_worker_update_asset_ingest", {
    p_asset: params.assetId,
    p_status: params.status,
    p_detail: params.detail ?? null,
    p_storage_path: params.storagePath ?? null,
  });
}

export async function vrfSaveAssetAnalysis(params: {
  assetId: string;
  breakdown?: unknown;
  originalScore?: number | null;
  probability?: number | null;
  confidence?: string;
  duration?: number | null;
  thumbnailUrl?: string;
  title?: string;
}) {
  return rpc("vrf_worker_save_asset_analysis", {
    p_asset: params.assetId,
    p_breakdown: params.breakdown ?? {},
    p_original_score: params.originalScore ?? null,
    p_probability: params.probability ?? null,
    p_confidence: params.confidence ?? "",
    p_duration: params.duration ?? null,
    p_thumbnail: params.thumbnailUrl ?? null,
    p_title: params.title ?? null,
  });
}

export async function vrfSaveProcessing(params: {
  assetId: string;
  processedPath: string;
  duration?: number | null;
  width?: number | null;
  height?: number | null;
  plan?: unknown;
  originalScore?: number | null;
  probability?: number | null;
  confidence?: string;
  breakdown?: unknown;
}) {
  return rpc("vrf_worker_save_processing", {
    p_asset: params.assetId,
    p_processed_path: params.processedPath,
    p_duration: params.duration ?? null,
    p_width: params.width ?? null,
    p_height: params.height ?? null,
    p_plan: params.plan ?? {},
    p_original_score: params.originalScore ?? null,
    p_probability: params.probability ?? null,
    p_confidence: params.confidence ?? "",
    p_breakdown: params.breakdown ?? {},
  });
}

export async function vrfPostsToMeasure(limit = 25) {
  return (await rpc("vrf_worker_posts_to_measure", { p_limit: limit })) ?? [];
}

export async function vrfRecordMetric(params: {
  userId: string;
  jobId: string;
  platform: string;
  views?: number | null;
  likes?: number | null;
  comments?: number | null;
  shares?: number | null;
  engagementRate?: number | null;
  predicted?: number | null;
}) {
  return rpc("vrf_worker_record_metric", {
    p_user: params.userId,
    p_job: params.jobId,
    p_platform: params.platform,
    p_views: params.views ?? null,
    p_likes: params.likes ?? null,
    p_comments: params.comments ?? null,
    p_shares: params.shares ?? null,
    p_engagement: params.engagementRate ?? null,
    p_predicted: params.predicted ?? null,
  });
}
