/**
 * Publish request validation and planning.
 *
 * Mirrors `viral/publishing.py` (`validate_request`, `plan_publication`,
 * `caption_for`). It lives on this side too so the user is told about an
 * unsupported platform or an impossible schedule while they are still in the
 * form, rather than in a job that fails hours later. The Python worker
 * re-checks everything before it posts; this is the earlier of two gates, not
 * the only one.
 */

import { PLATFORMS } from "./vrf";

export const CAPTION_LIMITS: Record<string, number> = {
  youtube: 5000,
  instagram: 2200,
  facebook: 5000,
  tiktok: 2200,
  snapchat: 250,
};

export const MAX_SCHEDULE_DAYS = 180;

export interface PublishInput {
  assetId: string;
  caption: string;
  platforms: string[];
  scheduledFor: string | null;
  attribution: string;
}

export interface PlannedJob {
  platform: string;
  status: "queued" | "unsupported";
  mode: "publish_now" | "native_schedule" | "queue_until_due";
  caption: string;
  deliver_at: string | null;
  next_attempt_at: string | null;
  reason: string;
}

function capability(platform: string) {
  return PLATFORMS.find((p) => p.platform === platform);
}

/** Caption with attribution, trimmed to the platform's limit. */
export function captionFor(
  caption: string,
  attribution: string,
  platform: string,
): string {
  let text = (caption ?? "").trim();
  const credit = (attribution ?? "").trim();
  if (credit && !text.toLowerCase().includes(credit.toLowerCase())) {
    text = `${text}\n\n${credit}`.trim();
  }
  return text.slice(0, CAPTION_LIMITS[platform] ?? 2200);
}

/** Everything wrong with this request, found before anything is queued. */
export function validatePublish(input: PublishInput, now = new Date()): string[] {
  const problems: string[] = [];
  if (!input.assetId?.trim()) problems.push("Choose a video to publish.");
  if (!input.platforms?.length) problems.push("Select at least one platform.");

  for (const platform of input.platforms ?? []) {
    const cap = capability(platform);
    if (!cap) {
      problems.push(`Unknown platform '${platform}'.`);
      continue;
    }
    if (!cap.canPublish) problems.push(`${cap.label}: ${cap.note}`);
  }

  if (input.scheduledFor) {
    const when = new Date(input.scheduledFor);
    if (Number.isNaN(when.getTime())) {
      problems.push("That scheduled time is not a valid date.");
    } else if (when <= now) {
      problems.push("Scheduled time is in the past.");
    } else if (
      when.getTime() >
      now.getTime() + MAX_SCHEDULE_DAYS * 24 * 60 * 60 * 1000
    ) {
      problems.push(`Scheduled time is more than ${MAX_SCHEDULE_DAYS} days out.`);
    }
  }
  return problems;
}

/**
 * One job per platform, with how each will actually be delivered.
 *
 * A platform that cannot do what was asked is planned as `unsupported` here so
 * it shows immediately, rather than being queued and failing later.
 */
export function planPublish(input: PublishInput, now = new Date()): PlannedJob[] {
  const scheduled = input.scheduledFor ? new Date(input.scheduledFor) : null;

  return (input.platforms ?? []).map((platform) => {
    const cap = capability(platform);
    if (!cap || !cap.canPublish) {
      return {
        platform,
        status: "unsupported" as const,
        mode: "publish_now" as const,
        caption: "",
        deliver_at: null,
        next_attempt_at: null,
        reason: cap?.note ?? `Unknown platform '${platform}'.`,
      };
    }

    let mode: PlannedJob["mode"] = "publish_now";
    let nextAttempt: string | null = now.toISOString();
    if (scheduled && cap.nativeScheduling) {
      // The platform is told the time at upload, so the job itself runs now.
      mode = "native_schedule";
    } else if (scheduled) {
      mode = "queue_until_due";
      nextAttempt = scheduled.toISOString();
    }

    return {
      platform,
      status: "queued" as const,
      mode,
      caption: captionFor(input.caption, input.attribution, platform),
      deliver_at: scheduled ? scheduled.toISOString() : null,
      next_attempt_at: nextAttempt,
      reason: "",
    };
  });
}
