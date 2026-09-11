/**
 * What each platform can actually do with a carousel, and what this repo can
 * actually do about it today.
 *
 * Written as a capability matrix rather than four `if` branches because the
 * four platforms genuinely differ, and the differences are the interesting
 * part:
 *
 *   Instagram  a real carousel: N image containers, then one CAROUSEL parent
 *   Facebook   N unpublished photos, then one feed post with attached_media
 *   Threads    a carousel, but a shorter one and its own Meta app
 *   X          up to 4 images on one post -- fewer than a carousel usually has
 *
 * So "publish the carousel" is not one operation. Where a platform cannot
 * carry the exact format, the honest move is to say what it will carry, not
 * to silently post slide one and call it done.
 *
 * IMPORTANT: `implemented` is false everywhere on purpose. The existing
 * publish adapters in `viral/adapters.py` are video-only -- Instagram posts
 * `media_type: REELS`, Facebook posts to `/videos` -- and none of them can
 * post an image. Marking a platform ready before that exists would be the
 * one thing this file is for preventing.
 */

export type PublishPlatform = "instagram" | "facebook" | "threads" | "x";

export interface PlatformCarouselCapability {
  platform: PublishPlatform;
  label: string;
  /** Whether the official API supports a true multi-image post. */
  supportsCarousel: boolean;
  /** Most images the platform will accept in one post. */
  maxImages: number;
  /** Whether THIS repo can publish it today. */
  implemented: boolean;
  /** Whether OAuth for this platform already exists in the repo. */
  oauthReady: boolean;
  /** What the official API calls this, for the report and the UI note. */
  format: string;
  /** What is still missing, in plain words. */
  missing: string;
}

export const CAROUSEL_PLATFORMS: PlatformCarouselCapability[] = [
  {
    platform: "instagram",
    label: "Instagram",
    supportsCarousel: true,
    maxImages: 10,
    implemented: false,
    oauthReady: true,
    format: "Carousel container (IMAGE children + CAROUSEL parent)",
    missing:
      "The existing Instagram adapter publishes REELS video only. A carousel " +
      "needs image containers, and the Graph API fetches each image by URL, " +
      "so rendered slides must be hosted first.",
  },
  {
    platform: "facebook",
    label: "Facebook",
    supportsCarousel: true,
    maxImages: 10,
    implemented: false,
    oauthReady: true,
    format: "Unpublished /photos, then a /feed post with attached_media",
    missing:
      "The existing Facebook adapter posts to /videos. Multi-photo posting " +
      "and image hosting are not built.",
  },
  {
    platform: "threads",
    label: "Threads",
    supportsCarousel: true,
    maxImages: 20,
    implemented: false,
    oauthReady: false,
    format: "CAROUSEL container with IMAGE children",
    missing:
      "No Threads OAuth provider and no adapter exist. Threads uses its own " +
      "Meta app and its own API host, separate from Instagram and Facebook.",
  },
  {
    platform: "x",
    label: "X",
    supportsCarousel: false,
    maxImages: 4,
    implemented: false,
    oauthReady: false,
    format: "Up to 4 images attached to one post",
    missing:
      "No X OAuth provider and no adapter exist. X has no carousel: a post " +
      "carries at most four images, so a longer carousel cannot be posted " +
      "whole.",
  },
];

export function capability(platform: string): PlatformCarouselCapability | null {
  return CAROUSEL_PLATFORMS.find((p) => p.platform === platform) ?? null;
}

/** Whether anything at all can be published right now. */
export function anyPlatformImplemented(): boolean {
  return CAROUSEL_PLATFORMS.some((p) => p.implemented);
}

/**
 * What a platform will actually carry, given a slide count.
 *
 * Returned for display *before* the user selects anything, so a four-image
 * limit is visible while choosing rather than discovered afterwards.
 */
export function coverageNote(
  platform: PublishPlatform,
  slides: number,
): string {
  const spec = capability(platform);
  if (!spec) return "";
  if (slides <= spec.maxImages) return "";
  return spec.supportsCarousel
    ? `Only the first ${spec.maxImages} slides fit one ${spec.label} post.`
    : `${spec.label} carries ${spec.maxImages} images per post, not ${slides}.`;
}

// ── connection state ─────────────────────────────────────────────────

export interface ConnectionState {
  platform: PublishPlatform;
  label: string;
  connected: boolean;
  handle: string;
  /** True when the platform could be connected today, if the user chose to. */
  connectable: boolean;
  capability: PlatformCarouselCapability;
}

/**
 * Real connection state from the account rows.
 *
 * Deliberately takes the rows rather than fetching: the caller already reads
 * them through the user's own session, and a component that fetched its own
 * would be a second place for "connected" to mean something different.
 *
 * A revoked account is not connected. That distinction matters -- a row
 * exists, so a naive check would show a green tick for an account that can no
 * longer post.
 */
export function connectionStates(
  accounts: { platform: string; account_handle?: string; revoked_at?: string | null }[],
): ConnectionState[] {
  return CAROUSEL_PLATFORMS.map((spec) => {
    const row = (accounts ?? []).find(
      (a) => a.platform === spec.platform && !a.revoked_at,
    );
    return {
      platform: spec.platform,
      label: spec.label,
      connected: Boolean(row),
      handle: String(row?.account_handle ?? ""),
      connectable: spec.oauthReady,
      capability: spec,
    };
  });
}

/**
 * Whether a publish may be attempted for this platform.
 *
 * Three independent gates, and all three must pass. A disconnected account
 * cannot publish; an unimplemented platform cannot publish however connected
 * it is; and nothing publishes without the user's explicit confirmation.
 */
export function canPublish(
  state: ConnectionState,
  confirmed: boolean,
): { allowed: boolean; reason: string } {
  if (!state.connected) {
    return { allowed: false, reason: `Connect ${state.label} first.` };
  }
  if (!state.capability.implemented) {
    return {
      allowed: false,
      reason: `Publishing to ${state.label} is not available yet.`,
    };
  }
  if (!confirmed) {
    return { allowed: false, reason: "Confirm before publishing." };
  }
  return { allowed: true, reason: "" };
}

// ── captions ─────────────────────────────────────────────────────────

/**
 * One caption, shaped for a platform.
 *
 * Shaping is length and framing only. The meaning is not rewritten: a caption
 * that says something different on X than on Instagram is two posts, not one
 * carousel published twice.
 */
export function adaptCaption(
  caption: string,
  platform: PublishPlatform,
  limit: number,
): string {
  const text = String(caption ?? "").trim();
  if (!text) return "";
  if (text.length <= limit) return text;

  // Cut at a sentence end where possible, so a trimmed caption still reads as
  // a finished thought rather than a severed one.
  const room = text.slice(0, limit);
  const stop = Math.max(
    room.lastIndexOf("."),
    room.lastIndexOf("؟"),
    room.lastIndexOf("!"),
    room.lastIndexOf("\n"),
  );
  if (stop > limit * 0.5) return room.slice(0, stop + 1).trim();
  const space = room.lastIndexOf(" ");
  return (space > limit * 0.5 ? room.slice(0, space) : room).trim();
}
