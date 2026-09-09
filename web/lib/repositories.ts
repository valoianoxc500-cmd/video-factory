/**
 * Repository layer.
 *
 * Routes and pages talk to these; nothing above this file knows that the
 * records live in Postgres or that the bytes live in GCS. Swapping either
 * backend means reimplementing here and changing nothing else.
 *
 * Every method takes an authenticated Supabase client whose session carries
 * the caller's identity. Ownership is enforced by RLS underneath, so these
 * queries cannot return another user's rows even if a filter were forgotten --
 * `user_id` is never read from a request body.
 */

import type { SupabaseClient } from "@supabase/supabase-js";
import { customerJobMessage, customerSafeError } from "./customer-errors";

/** Channel slugs the pipeline can actually run. */
export const CHANNELS = [
  { slug: "horror_stories", name: "Horror Stories", theme: "horror" },
  { slug: "football_news", name: "Football News", theme: "news" },
] as const;

export type ChannelSlug = (typeof CHANNELS)[number]["slug"];

const SLUG_RE = /^[a-z0-9_]{1,64}$/;

export function isKnownChannel(slug: string): slug is ChannelSlug {
  return CHANNELS.some((c) => c.slug === slug);
}

export function assertChannelSlug(slug: string): ChannelSlug {
  if (!SLUG_RE.test(slug) || !isKnownChannel(slug)) {
    throw new ValidationError("Unknown channel.");
  }
  return slug;
}

export class ValidationError extends Error {
  readonly status = 400;
  constructor(message: string) {
    super(message);
    this.name = "ValidationError";
  }
}

export class NotFoundError extends Error {
  // 404 rather than 403 on someone else's row: confirming a record exists but
  // belongs to another account is itself a disclosure.
  readonly status = 404;
  constructor(message = "Not found.") {
    super(message);
    this.name = "NotFoundError";
  }
}

// ── videos ────────────────────────────────────────────────────

export interface VideoRow {
  id: string;
  channel_slug: string;
  video_key: string;
  title: string;
  description: string;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  fps: number | null;
  video_path: string;
  thumbnail_path: string | null;
  review_status: string;
  created_at: string;
}

const VIDEO_FIELDS =
  "id, channel_slug, video_key, title, description, duration_seconds, " +
  "width, height, fps, video_path, thumbnail_path, review_status, created_at";

export class VideoRepository {
  constructor(private readonly db: SupabaseClient) {}

  async listForUser(channel?: string): Promise<VideoRow[]> {
    let query = this.db
      .from("videos")
      .select(VIDEO_FIELDS)
      .order("created_at", { ascending: false });
    if (channel) query = query.eq("channel_slug", assertChannelSlug(channel));
    const { data, error } = await query;
    if (error) throw new RepositoryError(error.message);
    return (data ?? []) as unknown as VideoRow[];
  }

  /** One video the caller owns. RLS makes another user's id a miss, not a leak. */
  async getOwned(id: string): Promise<VideoRow> {
    const { data, error } = await this.db
      .from("videos")
      .select(VIDEO_FIELDS)
      .eq("id", id)
      .maybeSingle();
    if (error) throw new RepositoryError(error.message);
    if (!data) throw new NotFoundError("That video does not exist.");
    return data as unknown as VideoRow;
  }

  async countByChannel(): Promise<Record<string, number>> {
    const { data, error } = await this.db.from("videos").select("channel_slug");
    if (error) throw new RepositoryError(error.message);
    const counts: Record<string, number> = {};
    for (const row of data ?? []) {
      const slug = (row as { channel_slug: string }).channel_slug;
      counts[slug] = (counts[slug] ?? 0) + 1;
    }
    return counts;
  }
}

// ── jobs ──────────────────────────────────────────────────────

export interface JobRow {
  id: string;
  topic: string;
  status: "queued" | "running" | "done" | "error";
  progress: number;
  stage: string;
  message: string;
  title: string | null;
  error: string | null;
  channel_slug: string | null;
  style?: string | null;
  language?: string | null;
  /** Empty or absent when captions are in the spoken language. */
  caption_language?: string | null;
  created_at: string;
  updated_at: string;
}

/** Strip diagnostics at the customer repository boundary. Raw job columns
 * remain untouched for worker/admin diagnostics. */
function customerJob(row: JobRow): JobRow {
  return {
    ...row,
    message: customerJobMessage(row.status, row.stage, row.message),
    error: row.error ? customerSafeError(row.error) : null,
  };
}

const JOB_FIELDS =
  "id, topic, status, progress, stage, message, title, error, " +
  "channel_slug, style, language, caption_language, created_at, updated_at";

export class JobRepository {
  constructor(private readonly db: SupabaseClient) {}

  async listForUser(limit = 50): Promise<JobRow[]> {
    const { data, error } = await this.db
      .from("jobs")
      .select(JOB_FIELDS)
      .order("created_at", { ascending: false })
      .limit(limit);
    if (error) throw new RepositoryError(error.message);
    return ((data ?? []) as unknown as JobRow[]).map(customerJob);
  }

  async getOwned(id: string): Promise<JobRow> {
    const { data, error } = await this.db
      .from("jobs")
      .select(JOB_FIELDS)
      .eq("id", id)
      .maybeSingle();
    if (error) throw new RepositoryError(error.message);
    if (!data) throw new NotFoundError("That job does not exist.");
    return customerJob(data as unknown as JobRow);
  }

  /**
   * Fail jobs whose worker stopped reporting.
   *
   * The worker heartbeats while a stage runs, so a `running` row that has not
   * been touched for this long has no process behind it -- the worker was
   * killed, the machine rebooted, the render wedged. Nothing else notices:
   * the row stays `running` forever, the dashboard polls it forever, and
   * `activeForUser` keeps returning it, so the account cannot start anything
   * new either.
   *
   * This is the recovery. It runs on read, which is the one thing a stuck
   * user is guaranteed to do, and RLS scopes it to their own jobs.
   *
   * Deliberately generous: it must sit above the longest gap a healthy run
   * can leave between updates, or it would fail live work.
   */
  async expireStale(olderThanMinutes = 15): Promise<number> {
    const cutoff = new Date(
      Date.now() - olderThanMinutes * 60_000,
    ).toISOString();

    const { data, error } = await this.db
      .from("jobs")
      .update({
        // A queued job has a deterministic checkpoint workspace now, so a
        // stale heartbeat is a bounded recovery opportunity, not a terminal
        // customer failure. The worker resumes completed stages in place.
        status: "queued",
        stage: "queued",
        message: "Saved progress is ready to resume.",
        error: null,
        updated_at: new Date().toISOString(),
      })
      // Queued work can legitimately wait for capacity. Only a worker that
      // claimed a job and then stopped heartbeating needs recovery.
      .eq("status", "running")
      .lt("updated_at", cutoff)
      .select("id");

    if (error) {
      // Housekeeping must never fail the request that triggered it.
      console.error("expireStale failed:", error.message);
      return 0;
    }
    return (data ?? []).length;
  }

  /** The caller's in-flight job, if any. One generation at a time per user. */
  async activeForUser(): Promise<JobRow | null> {
    const { data, error } = await this.db
      .from("jobs")
      .select(JOB_FIELDS)
      .in("status", ["queued", "running"])
      .order("created_at", { ascending: false })
      .limit(1);
    if (error) throw new RepositoryError(error.message);
    const row = ((data ?? [])[0] as unknown) as JobRow | undefined;
    return row ? customerJob(row) : null;
  }

  /**
   * Queue a generation. `user_id` comes from the verified session, never from
   * the request body, so a caller cannot file work against another account.
   */
  async create(params: {
    userId: string;
    topic: string;
    channel: string;
    style?: string;
    language?: string;
    captionLanguage?: string;
  }): Promise<JobRow> {
    const topic = params.topic.trim();
    if (!topic) throw new ValidationError("A topic is required.");
    if (topic.length > 300) {
      throw new ValidationError("Topic must be 300 characters or fewer.");
    }
    const channel = assertChannelSlug(params.channel);
    const style = (params.style ?? "").trim();
    if (style && !/^[a-z0-9_]{1,64}$/.test(style)) {
      throw new ValidationError("Unknown story type.");
    }
    // Voice language: what the narrator speaks. Visual search runs in both
    // scripts regardless, so this never limits which photographs are reachable.
    const language = (params.language ?? "").trim().toLowerCase();
    if (language && !/^[a-z]{2}(-[a-z]{2})?$/i.test(language)) {
      throw new ValidationError("Unknown voice language.");
    }
    // Caption language. Stored only when it differs from the voice language:
    // equal to it, captions come from the narration's own transcript and every
    // word timing is measured, which is the better track and the default.
    const requestedCaption = (params.captionLanguage ?? "").trim().toLowerCase();
    if (requestedCaption && !/^[a-z]{2}(-[a-z]{2})?$/i.test(requestedCaption)) {
      throw new ValidationError("Unknown caption language.");
    }
    const captionLanguage =
      requestedCaption && requestedCaption !== language ? requestedCaption : "";

    const { data, error } = await this.db
      .from("jobs")
      .insert({
        id: crypto.randomUUID(),
        user_id: params.userId,
        topic,
        channel_slug: channel,
        engine: channel,
        // Empty string, not null: both columns are NOT NULL with an empty
        // default, so sending null failed every engine that has no sub-mode.
        style: style,
        language: language,
        caption_language: captionLanguage,
        status: "queued",
        progress: 0,
        stage: "queued",
        message: "Waiting for a worker to pick this up",
      })
      .select(JOB_FIELDS)
      .single();
    if (error) throw new RepositoryError(error.message);
    return data as unknown as JobRow;
  }
}

// ── channels ──────────────────────────────────────────────────

export interface ChannelSummary {
  slug: string;
  name: string;
  theme: string;
  videoCount: number;
}

export class ChannelRepository {
  constructor(private readonly db: SupabaseClient) {}

  /** The channels available to this user, with their own video counts. */
  async listForUser(): Promise<ChannelSummary[]> {
    const counts = await new VideoRepository(this.db).countByChannel();
    return CHANNELS.map((c) => ({
      slug: c.slug,
      name: c.name,
      theme: c.theme,
      videoCount: counts[c.slug] ?? 0,
    }));
  }
}

export class RepositoryError extends Error {
  readonly status = 500;
  constructor(detail: string) {
    // Deliberately generic outward: a driver message can name tables, columns
    // and connection details. The detail goes to the server log instead.
    super("Something went wrong reading your data.");
    this.name = "RepositoryError";
    console.error("repository error:", detail);
  }
}

/** Map any thrown error to a safe status and message for a response. */
export function toHttpError(err: unknown): { status: number; message: string } {
  if (
    err instanceof ValidationError ||
    err instanceof NotFoundError ||
    err instanceof RepositoryError
  ) {
    return { status: err.status, message: err.message };
  }
  if (err && typeof err === "object" && "status" in err) {
    const status = Number((err as { status: unknown }).status);
    if (status === 401) {
      return { status: 401, message: "You must be signed in to do that." };
    }
  }
  console.error("unhandled error:", err);
  return { status: 500, message: "Something went wrong." };
}
