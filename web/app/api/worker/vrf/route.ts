import { isWorkerAuthorized } from "@/lib/worker-db";
import {
  vrfAccount,
  vrfAsset,
  vrfClaimPublishJob,
  vrfClaimTask,
  vrfCompleteTask,
  vrfPostsToMeasure,
  vrfRecordMetric,
  vrfSaveAnalysis,
  vrfSaveAssetAnalysis,
  vrfSaveProcessing,
  vrfUpdateAssetIngest,
  vrfSource,
  vrfStoreTokens,
  vrfUpdatePublishJob,
} from "@/lib/vrf-worker-db";

/**
 * The Viral Reels Finder worker's single endpoint.
 *
 * One route with an `action` rather than eight near-identical files. The
 * bearer token is checked here, and again inside each database function --
 * the second check is the one that actually authorises the row change.
 *
 * Note what this route does not do: it never decrypts a token. Ciphertext
 * goes out to the worker, which holds the key; nothing readable passes
 * through this deployment.
 */

export const dynamic = "force-dynamic";
export const maxDuration = 30;

export async function POST(request: Request) {
  if (!isWorkerAuthorized(request)) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  let body: Record<string, unknown>;
  try {
    body = ((await request.json()) ?? {}) as Record<string, unknown>;
  } catch {
    return Response.json({ error: "Invalid request body." }, { status: 400 });
  }

  const action = String(body.action ?? "");
  const get = (key: string) => body[key];

  try {
    switch (action) {
      case "claim_task":
        return Response.json({ task: await vrfClaimTask() });

      case "complete_task":
        await vrfCompleteTask({
          id: String(get("id")),
          status: String(get("status")) as "done" | "failed" | "queued",
          result: get("result") ?? {},
          error: String(get("error") ?? ""),
        });
        return Response.json({ ok: true });

      case "claim_publish_job":
        return Response.json({ job: await vrfClaimPublishJob() });

      case "update_publish_job":
        await vrfUpdatePublishJob({
          id: String(get("id")),
          status: String(get("status")),
          attempts: numberOrNull(get("attempts")),
          nextAttemptAt: stringOrNull(get("next_attempt_at")),
          postId: stringOrNull(get("post_id")),
          postUrl: stringOrNull(get("post_url")),
          error: stringOrNull(get("error")),
        });
        return Response.json({ ok: true });

      case "account":
        return Response.json({
          account: await vrfAccount(String(get("user_id")), String(get("platform"))),
        });

      case "store_tokens":
        await vrfStoreTokens({
          accountId: String(get("account_id")),
          accessTokenEncrypted: String(get("access_token_encrypted")),
          refreshTokenEncrypted: stringOrNull(get("refresh_token_encrypted")),
          expiresAt: stringOrNull(get("token_expires_at")),
        });
        return Response.json({ ok: true });

      case "source":
        return Response.json({ source: await vrfSource(String(get("id"))) });

      case "save_analysis":
        await vrfSaveAnalysis(String(get("id")), get("analysis") ?? {});
        return Response.json({ ok: true });

      case "asset":
        return Response.json({ asset: await vrfAsset(String(get("id"))) });

      case "update_asset_ingest":
        await vrfUpdateAssetIngest({
          assetId: String(get("id")),
          status: String(get("status")),
          detail: stringOrNull(get("detail")),
          storagePath: stringOrNull(get("storage_path")),
        });
        return Response.json({ ok: true });

      case "save_asset_analysis":
        await vrfSaveAssetAnalysis({
          assetId: String(get("id")),
          breakdown: get("score_breakdown") ?? {},
          originalScore: numberOrNull(get("original_viral_score")),
          probability: numberOrNull(get("new_version_probability")),
          confidence: String(get("probability_confidence") ?? ""),
          duration: numberOrNull(get("duration_seconds")),
          thumbnailUrl: String(get("thumbnail_url") ?? ""),
          title: String(get("title") ?? ""),
        });
        return Response.json({ ok: true });

      case "save_processing":
        await vrfSaveProcessing({
          assetId: String(get("id")),
          processedPath: String(get("processed_path") ?? ""),
          duration: numberOrNull(get("duration_seconds")),
          width: numberOrNull(get("width")),
          height: numberOrNull(get("height")),
          plan: get("processing_plan") ?? {},
          originalScore: numberOrNull(get("original_viral_score")),
          probability: numberOrNull(get("new_version_probability")),
          confidence: String(get("probability_confidence") ?? ""),
          breakdown: get("score_breakdown") ?? {},
        });
        return Response.json({ ok: true });

      case "posts_to_measure":
        return Response.json({
          posts: await vrfPostsToMeasure(numberOrNull(get("limit")) ?? 25),
        });

      case "record_metric":
        return Response.json({
          id: await vrfRecordMetric({
            userId: String(get("user_id")),
            jobId: String(get("job_id")),
            platform: String(get("platform")),
            views: numberOrNull(get("views")),
            likes: numberOrNull(get("likes")),
            comments: numberOrNull(get("comments")),
            shares: numberOrNull(get("shares")),
            engagementRate: numberOrNull(get("engagement_rate")),
            predicted: numberOrNull(get("predicted_probability")),
          }),
        });

      default:
        return Response.json({ error: `Unknown action '${action}'.` }, { status: 400 });
    }
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error(`vrf worker action ${action} failed:`, message);
    return Response.json({ error: "Worker action failed." }, { status: 503 });
  }
}

function numberOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function stringOrNull(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  return String(value);
}
