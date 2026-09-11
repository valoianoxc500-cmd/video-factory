/**
 * Viral Reels Finder repositories.
 *
 * Same contract as `lib/repositories.ts`: every method takes an authenticated
 * Supabase client, `user_id` is never read from a request body, and RLS on the
 * `vrf_*` tables is the backstop underneath. A user cannot reach another
 * user's discoveries, assets, accounts, jobs or metrics through this file.
 *
 * Work that needs an external API or a model -- discovery, AI analysis, video
 * processing -- is enqueued as a `vrf_tasks` row for the Python worker rather
 * than run in a request handler. Publishing has its own table because it
 * carries scheduling and retry state.
 */

import type { SupabaseClient } from "@supabase/supabase-js";
import { NotFoundError, ValidationError, toHttpError } from "./repositories";
import { ConnectError } from "./vrf-oauth";
import { TokenSecurityError } from "./vrf-crypto";
import {
  UnsupportedUrlError,
  mediaImportPlan,
  parseVideoUrl,
} from "./vrf-ingest";

/**
 * Error mapping for the reels routes.
 *
 * `toHttpError` deliberately swallows unknown errors into a generic 500 so a
 * driver's message never reaches a user. Connect and encryption failures are
 * different: their messages are written for the user and say what to do next,
 * so they are passed through with their own status.
 */
export function toReelsHttpError(err: unknown): { status: number; message: string } {
  if (
    err instanceof ConnectError ||
    err instanceof TokenSecurityError ||
    // The message names the platform and what to paste instead, which is the
    // whole point of rejecting the link.
    err instanceof UnsupportedUrlError
  ) {
    return { status: err.status, message: err.message };
  }
  return toHttpError(err);
}

// â”€â”€ platform capabilities â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
// Mirrors viral/publishing.py CAPABILITIES. tests/test_viral_web_parity.py
// fails if the two drift, because a UI that offers a platform the backend
// refuses is worse than one that never offered it.

export interface PlatformCapability {
  platform: string;
  label: string;
  canPublish: boolean;
  nativeScheduling: boolean;
  draftOnly: boolean;
  supported: boolean;
  requires: string;
  note: string;
}

/**
 * Video publishing destinations.
 *
 * This list is the Reels queue's world: `viral/publishing.py` mirrors it,
 * the adapters are keyed by it, and anything listed here is a platform the
 * pipeline will eventually try to send an mp4 to. It is therefore the wrong
 * place for an account that exists only to receive images -- see
 * `CONNECT_ONLY_PLATFORMS` below.
 */
export const PLATFORMS: PlatformCapability[] = [
  {
    platform: "youtube",
    label: "YouTube Shorts",
    canPublish: true,
    nativeScheduling: true,
    draftOnly: false,
    supported: true,
    requires: "YouTube Data API v3, youtube.upload scope",
    note: "Scheduled publishing is native via status.publishAt.",
  },
  {
    platform: "instagram",
    label: "Instagram Reels",
    canPublish: true,
    nativeScheduling: false,
    draftOnly: false,
    supported: true,
    requires: "Instagram Graph API, Business or Creator account",
    note:
      "Two-step container publish. No native scheduling, so the queue holds " +
      "the job until its scheduled time.",
  },
  {
    platform: "facebook",
    label: "Facebook",
    canPublish: true,
    nativeScheduling: true,
    draftOnly: false,
    supported: true,
    requires: "Facebook Graph API, Page access token",
    note: "",
  },
  {
    platform: "tiktok",
    label: "TikTok",
    canPublish: true,
    nativeScheduling: false,
    draftOnly: false,
    supported: true,
    requires: "TikTok Content Posting API, audited app for Direct Post",
    note:
      "Unaudited apps can only send to the user's drafts; Direct Post needs " +
      "an approved audit.",
  },
  {
    platform: "snapchat",
    label: "Snapchat",
    canPublish: false,
    nativeScheduling: false,
    draftOnly: false,
    supported: false,
    requires: "",
    note:
      "Snapchat has no server-side publishing API. Creative Kit is an " +
      "app-to-app share from a mobile client and cannot be driven by a backend.",
  },
];

/**
 * Accounts a user may connect that are NOT video publishing destinations.
 *
 * Threads and X are connected so Quote Studio can publish image carousels to
 * them. They are deliberately not in `PLATFORMS`: that list is what the Reels
 * queue iterates, and an image-only account enrolled there would eventually
 * be handed an mp4 by a video adapter that has no idea it cannot accept one.
 *
 * Keeping them in a second list is what lets the Accounts page offer a
 * Connect button without that button meaning "publish my videos here".
 * `canPublish` is false throughout for the same reason -- it is read as
 * "can publish *video*".
 */
export const CONNECT_ONLY_PLATFORMS: PlatformCapability[] = [
  {
    platform: "threads",
    label: "Threads",
    canPublish: false,
    nativeScheduling: false,
    draftOnly: false,
    supported: true,
    requires: "A Meta Threads app (separate from the Instagram/Facebook app)",
    note: "Connected for Quote Studio image carousels. Not a video destination.",
  },
  {
    platform: "x",
    label: "X",
    canPublish: false,
    nativeScheduling: false,
    draftOnly: false,
    supported: true,
    requires: "An X developer app with OAuth 2.0 and media upload",
    note:
      "Connected for Quote Studio image posts, up to four images. Not a " +
      "video destination.",
  },
];

/** Everything the Accounts page may offer a Connect button for. */
export const CONNECTABLE_PLATFORMS: PlatformCapability[] = [
  ...PLATFORMS,
  ...CONNECT_ONLY_PLATFORMS,
];

export const PUBLISHABLE_PLATFORMS = PLATFORMS.filter((p) => p.supported).map(
  (p) => p.platform,
);

/** Rights bases a user can attest to. `discovered` is deliberately absent. */
export const RIGHTS_SOURCES = [
  {
    // What the Add-a-video form actually collects: one confirmation covering
    // both bases. Kept as its own value rather than mapped onto own_recording,
    // so the stored attestation matches what the user was asked.
    value: "owned_or_permitted",
    label: "I own this content or have permission to reuse it",
  },
  { value: "own_recording", label: "I recorded or produced it" },
  { value: "licensed", label: "I licensed it", needsEvidence: true },
  {
    // Matches viral.rights.Source.WRITTEN_PERMISSION and the vrf_assets
    // CHECK constraint, both of which spell it "permission".
    value: "permission",
    label: "The creator gave me written permission",
    needsHolder: true,
    needsEvidence: true,
  },
  {
    value: "creative_commons",
    label: "Creative Commons (credit required)",
    needsHolder: true,
  },
  { value: "public_domain", label: "Public domain" },
] as const;

export type TaskKind =
  | "discover"
  | "analyse"
  | "ingest"
  | "process"
  | "collect_metrics"
  // Clip Analyzer. Reads a video and explains it; writes nothing back onto
  // the asset and queues no generation, so the result lives on the task.
  | "explain";

// â”€â”€ discovery tasks â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

export interface TaskRow {
  id: string;
  kind: TaskKind;
  status: "queued" | "running" | "done" | "failed";
  payload: Record<string, unknown>;
  result: Record<string, unknown>;
  error: string;
  created_at: string;
  completed_at: string | null;
}

const TASK_FIELDS =
  "id, kind, status, payload, result, error, created_at, completed_at";

export class TaskRepository {
  constructor(private readonly db: SupabaseClient) {}

  async create(
    userId: string,
    kind: TaskKind,
    payload: Record<string, unknown>,
  ): Promise<TaskRow> {
    const { data, error } = await this.db
      .from("vrf_tasks")
      .insert({ user_id: userId, kind, payload })
      .select(TASK_FIELDS)
      .single();
    if (error) throw new Error(error.message);
    return data as unknown as TaskRow;
  }

  async get(id: string): Promise<TaskRow> {
    const { data, error } = await this.db
      .from("vrf_tasks")
      .select(TASK_FIELDS)
      .eq("id", id)
      .maybeSingle();
    if (error) throw new Error(error.message);
    if (!data) throw new NotFoundError("No such task.");
    return data as unknown as TaskRow;
  }

  async latest(kind: TaskKind): Promise<TaskRow | null> {
    const { data, error } = await this.db
      .from("vrf_tasks")
      .select(TASK_FIELDS)
      .eq("kind", kind)
      .order("created_at", { ascending: false })
      .limit(1);
    if (error) throw new Error(error.message);
    return (data?.[0] as unknown as TaskRow) ?? null;
  }

  /** Has this user got work already running? Keeps one search per person. */
  async activeCount(kind: TaskKind): Promise<number> {
    const { count, error } = await this.db
      .from("vrf_tasks")
      .select("id", { count: "exact", head: true })
      .eq("kind", kind)
      .in("status", ["queued", "running"]);
    if (error) throw new Error(error.message);
    return count ?? 0;
  }
}

// â”€â”€ discovered videos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

export interface SourceRow {
  id: string;
  platform: string;
  video_id: string;
  url: string;
  title: string;
  author: string;
  thumbnail_url: string;
  niche: string;
  views: number | null;
  likes: number | null;
  comments: number | null;
  followers: number | null;
  posted_at: string | null;
  duration_seconds: number | null;
  viral_score: number | null;
  score_confidence: string;
  score_breakdown: Record<string, unknown>;
  analysis: Record<string, unknown>;
  discovered_at: string;
}

const SOURCE_FIELDS =
  "id, platform, video_id, url, title, author, thumbnail_url, niche, views, " +
  "likes, comments, followers, posted_at, duration_seconds, viral_score, " +
  "score_confidence, score_breakdown, analysis, discovered_at";

export class SourceRepository {
  constructor(private readonly db: SupabaseClient) {}

  async listForUser(limit = 60): Promise<SourceRow[]> {
    const { data, error } = await this.db
      .from("vrf_sources")
      .select(SOURCE_FIELDS)
      .order("viral_score", { ascending: false, nullsFirst: false })
      .limit(limit);
    if (error) throw new Error(error.message);
    return (data ?? []) as unknown as SourceRow[];
  }

  async save(userId: string, video: Record<string, unknown>): Promise<SourceRow> {
    const platform = String(video.platform ?? "").trim();
    const videoId = String(video.video_id ?? "").trim();
    if (!platform || !videoId) {
      throw new ValidationError("A saved video needs a platform and an id.");
    }
    const row = {
      user_id: userId,
      platform,
      video_id: videoId,
      url: String(video.url ?? ""),
      title: String(video.title ?? ""),
      author: String(video.author ?? ""),
      thumbnail_url: String(video.thumbnail_url ?? ""),
      niche: String(video.niche ?? ""),
      language: String(video.language ?? ""),
      views: numberOrNull(video.views),
      likes: numberOrNull(video.likes),
      comments: numberOrNull(video.comments),
      shares: numberOrNull(video.shares),
      followers: numberOrNull(video.followers),
      posted_at: video.posted_at ? String(video.posted_at) : null,
      duration_seconds: numberOrNull(video.duration_seconds),
      viral_score: numberOrNull(video.viral_score),
      score_confidence: String(video.score_confidence ?? ""),
      score_breakdown: (video.score_breakdown ?? {}) as Record<string, unknown>,
    };
    const { data, error } = await this.db
      .from("vrf_sources")
      .upsert(row, { onConflict: "user_id,platform,video_id" })
      .select(SOURCE_FIELDS)
      .single();
    if (error) throw new Error(error.message);
    return data as unknown as SourceRow;
  }

  async remove(id: string): Promise<void> {
    const { error } = await this.db.from("vrf_sources").delete().eq("id", id);
    if (error) throw new Error(error.message);
  }
}

// â”€â”€ the user's own videos â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

export interface AssetRow {
  id: string;
  title: string;
  storage_path: string;
  processed_path: string;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  rights_source: string;
  rights_holder: string;
  needs_attribution: boolean;
  original_viral_score: number | null;
  new_version_probability: number | null;
  probability_confidence: string;
  created_at: string;
  source_url: string;
  source_platform: string;
  source_video_id: string;
  source_author: string;
  thumbnail_url: string;
  ingest_status: string;
  ingest_detail: string;
  publish_state: string;
}

const ASSET_FIELDS =
  "id, title, storage_path, processed_path, duration_seconds, width, height, " +
  "rights_source, rights_holder, needs_attribution, original_viral_score, " +
  "new_version_probability, probability_confidence, created_at, " +
  "source_url, source_platform, source_video_id, source_author, " +
  "thumbnail_url, ingest_status, ingest_detail, publish_state";

export class AssetRepository {
  constructor(private readonly db: SupabaseClient) {}

  async listForUser(): Promise<AssetRow[]> {
    const { data, error } = await this.db
      .from("vrf_assets")
      .select(ASSET_FIELDS)
      .order("created_at", { ascending: false });
    if (error) throw new Error(error.message);
    return (data ?? []) as unknown as AssetRow[];
  }

  async get(id: string): Promise<AssetRow> {
    const { data, error } = await this.db
      .from("vrf_assets")
      .select(ASSET_FIELDS)
      .eq("id", id)
      .maybeSingle();
    if (error) throw new Error(error.message);
    if (!data) throw new NotFoundError("No such video.");
    return data as unknown as AssetRow;
  }

  /**
   * Record a video the user holds rights to.
   *
   * The rights basis is required and validated here as well as by the table's
   * CHECK constraint, because this is the gate the whole product depends on:
   * `discovered` is not a rights basis and must never become one.
   */
  async create(
    userId: string,
    input: {
      title: string;
      storagePath: string;
      rightsSource: string;
      rightsHolder?: string;
      rightsEvidence?: string;
      durationSeconds?: number | null;
    },
  ): Promise<AssetRow> {
    const source = input.rightsSource.trim().toLowerCase();
    const known = RIGHTS_SOURCES.find((r) => r.value === source);
    if (!known) {
      throw new ValidationError(
        "Choose how you hold the rights to this video. Videos found through " +
          "discovery cannot be published; they are reference material.",
      );
    }
    const holder = (input.rightsHolder ?? "").trim();
    const evidence = (input.rightsEvidence ?? "").trim();
    if ("needsHolder" in known && known.needsHolder && !holder) {
      throw new ValidationError("Name the rights holder to credit.");
    }
    if ("needsEvidence" in known && known.needsEvidence && !evidence) {
      throw new ValidationError(
        "Link or describe the licence or permission you hold.",
      );
    }
    if (!input.storagePath.trim()) {
      throw new ValidationError("Upload the video file first.");
    }

    const { data, error } = await this.db
      .from("vrf_assets")
      .insert({
        user_id: userId,
        title: input.title.trim() || "Untitled",
        storage_path: input.storagePath.trim(),
        rights_source: source,
        rights_holder: holder,
        rights_evidence: evidence,
        needs_attribution: source === "creative_commons",
        duration_seconds: input.durationSeconds ?? null,
      })
      .select(ASSET_FIELDS)
      .single();
    if (error) throw new Error(error.message);
    return data as unknown as AssetRow;
  }

  /**
   * Add a video from a pasted link -- the only path the UI now offers.
   *
   * The user supplies a URL and one confirmation. Everything else (platform,
   * video id, whether the media can legally be fetched) is derived here, so
   * the client cannot assert any of it: a request claiming "TikTok, media
   * importable" for a YouTube link is re-derived from the URL and corrected.
   *
   * Ownership comes from the session, never the body, exactly as before.
   */
  async createFromUrl(
    userId: string,
    input: {
      url: string;
      ownsOrPermitted: boolean;
      connectedPlatforms?: string[];
      title?: string;
      thumbnailUrl?: string;
      author?: string;
    },
  ): Promise<AssetRow> {
    if (!input.ownsOrPermitted) {
      throw new ValidationError(
        "Confirm you own this content or have permission to reuse it. " +
          "Nothing is processed or published without that.",
      );
    }

    // Throws UnsupportedUrlError (status 400) with a message for the user.
    const parsed = parseVideoUrl(input.url);
    const plan = mediaImportPlan(parsed, input.connectedPlatforms ?? []);

    // `metadata_only` is a real outcome, not an error: the platform has no
    // official route to the file and we will not take one it did not offer.
    const ingestStatus = plan.canFetchMedia ? "pending" : "metadata_only";

    const { data, error } = await this.db
      .from("vrf_assets")
      .insert({
        user_id: userId,
        title: (input.title ?? "").trim() || parsed.ingest.label + " video",
        storage_path: "",
        rights_source: "owned_or_permitted",
        rights_holder: (input.author ?? "").trim(),
        rights_evidence: "",
        needs_attribution: false,
        source_url: parsed.canonicalUrl,
        source_platform: parsed.platform,
        source_video_id: parsed.videoId,
        source_author: (input.author ?? "").trim(),
        thumbnail_url: (input.thumbnailUrl ?? "").trim(),
        ingest_status: ingestStatus,
        ingest_detail: plan.reason,
      })
      .select(ASSET_FIELDS)
      .single();

    if (error) {
      // The partial unique index makes re-adding the same video a conflict
      // rather than a duplicate row.
      if (/duplicate key|unique constraint/i.test(error.message)) {
        throw new ValidationError(
          "That video is already in My Videos.",
        );
      }
      throw new Error(error.message);
    }
    return data as unknown as AssetRow;
  }
}

// â”€â”€ publish jobs â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

export interface PublishJobRow {
  id: string;
  asset_id: string;
  platform: string;
  status: string;
  mode: string;
  caption: string;
  scheduled_for: string | null;
  attempts: number;
  next_attempt_at: string | null;
  post_url: string;
  error: string;
  created_at: string;
  updated_at: string;
}

const JOB_FIELDS =
  "id, asset_id, platform, status, mode, caption, scheduled_for, attempts, " +
  "next_attempt_at, post_url, error, created_at, updated_at";

export class PublishJobRepository {
  constructor(private readonly db: SupabaseClient) {}

  async listForUser(status?: string | string[]): Promise<PublishJobRow[]> {
    let query = this.db
      .from("vrf_publish_jobs")
      .select(JOB_FIELDS)
      .order("created_at", { ascending: false });
    if (Array.isArray(status)) query = query.in("status", status);
    else if (status) query = query.eq("status", status);
    const { data, error } = await query;
    if (error) throw new Error(error.message);
    return (data ?? []) as unknown as PublishJobRow[];
  }

  async scheduled(): Promise<PublishJobRow[]> {
    const { data, error } = await this.db
      .from("vrf_publish_jobs")
      .select(JOB_FIELDS)
      .eq("status", "queued")
      .not("scheduled_for", "is", null)
      .order("scheduled_for", { ascending: true });
    if (error) throw new Error(error.message);
    return (data ?? []) as unknown as PublishJobRow[];
  }

  async createMany(
    userId: string,
    assetId: string,
    jobs: Array<Record<string, unknown>>,
  ): Promise<PublishJobRow[]> {
    const rows = jobs.map((job) => ({
      user_id: userId,
      asset_id: assetId,
      platform: String(job.platform ?? ""),
      status: String(job.status ?? "queued"),
      mode: String(job.mode ?? "publish_now"),
      caption: String(job.caption ?? ""),
      scheduled_for: job.deliver_at ? String(job.deliver_at) : null,
      next_attempt_at: job.next_attempt_at ? String(job.next_attempt_at) : null,
      error: String(job.reason ?? ""),
    }));
    const { data, error } = await this.db
      .from("vrf_publish_jobs")
      .insert(rows)
      .select(JOB_FIELDS);
    if (error) throw new Error(error.message);
    return (data ?? []) as unknown as PublishJobRow[];
  }

  /** Stop a job that has not finished. Terminal jobs are left alone. */
  async cancel(id: string): Promise<PublishJobRow> {
    const { data, error } = await this.db
      .from("vrf_publish_jobs")
      .update({
        status: "failed",
        error: "Cancelled.",
        next_attempt_at: null,
        updated_at: new Date().toISOString(),
      })
      .eq("id", id)
      .in("status", ["queued", "processing"])
      .select(JOB_FIELDS)
      .maybeSingle();
    if (error) throw new Error(error.message);
    if (!data) throw new NotFoundError("That job is not running.");
    return data as unknown as PublishJobRow;
  }
}

// â”€â”€ connected accounts â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

export interface AccountRow {
  id: string;
  platform: string;
  account_handle: string;
  account_ref: string;
  scopes: string;
  token_expires_at: string | null;
  connected_at: string;
  revoked_at: string | null;
}

// Token columns are deliberately not selected anywhere in this file. Nothing
// above the worker ever needs the ciphertext, let alone the token.
const ACCOUNT_FIELDS =
  "id, platform, account_handle, account_ref, scopes, token_expires_at, " +
  "connected_at, revoked_at";

export class AccountRepository {
  constructor(private readonly db: SupabaseClient) {}

  async listForUser(): Promise<AccountRow[]> {
    const { data, error } = await this.db
      .from("vrf_accounts")
      .select(ACCOUNT_FIELDS)
      .is("revoked_at", null)
      .order("connected_at", { ascending: false });
    if (error) throw new Error(error.message);
    return (data ?? []) as unknown as AccountRow[];
  }

  async upsert(
    userId: string,
    input: {
      platform: string;
      accountHandle: string;
      accountRef: string;
      scopes: string[];
      accessTokenEncrypted: string;
      refreshTokenEncrypted: string;
      expiresAt: string | null;
    },
  ): Promise<AccountRow> {
    const { data, error } = await this.db
      .from("vrf_accounts")
      .upsert(
        {
          user_id: userId,
          platform: input.platform,
          account_handle: input.accountHandle,
          account_ref: input.accountRef,
          scopes: input.scopes.join(" "),
          access_token_encrypted: input.accessTokenEncrypted,
          refresh_token_encrypted: input.refreshTokenEncrypted,
          token_expires_at: input.expiresAt,
          revoked_at: null,
        },
        { onConflict: "user_id,platform,account_ref" },
      )
      .select(ACCOUNT_FIELDS)
      .single();
    if (error) throw new Error(error.message);
    return data as unknown as AccountRow;
  }

  /**
   * Disconnect. The token material is cleared rather than left in a revoked
   * row, so a leaked backup cannot be replayed.
   */
  async revoke(id: string): Promise<void> {
    const { error } = await this.db
      .from("vrf_accounts")
      .update({
        revoked_at: new Date().toISOString(),
        access_token_encrypted: "",
        refresh_token_encrypted: "",
      })
      .eq("id", id);
    if (error) throw new Error(error.message);
  }
}

// â”€â”€ metrics â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

export interface MetricRow {
  id: string;
  publish_job_id: string;
  platform: string;
  views: number | null;
  likes: number | null;
  comments: number | null;
  shares: number | null;
  engagement_rate: number | null;
  predicted_probability: number | null;
  collected_at: string;
}

export class MetricsRepository {
  constructor(private readonly db: SupabaseClient) {}

  async listForUser(limit = 500): Promise<MetricRow[]> {
    const { data, error } = await this.db
      .from("vrf_metrics")
      .select(
        "id, publish_job_id, platform, views, likes, comments, shares, " +
          "engagement_rate, predicted_probability, collected_at",
      )
      .order("collected_at", { ascending: false })
      .limit(limit);
    if (error) throw new Error(error.message);
    return (data ?? []) as unknown as MetricRow[];
  }
}

function numberOrNull(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

