/**
 * Recognising a social video URL, and being honest about what can be fetched.
 *
 * The product asks for one thing from the user -- a link -- and works out the
 * rest. Working it out has two halves, and only the first is easy:
 *
 *   1. Which platform is this, and which video? Pure string parsing.
 *   2. Can we actually obtain the media? Almost always no.
 *
 * The second half is where this file earns its place. Official platform APIs
 * do not hand out arbitrary users' video files:
 *
 *   * YouTube Data API v3 returns metadata only. There is no endpoint that
 *     returns the media, and downloading it anyway breaks the Terms of Service.
 *   * Instagram, TikTok and Facebook return media only for accounts the user
 *     has connected and authorised, and then only for that account's own posts.
 *
 * So media import works for one case -- the user's own content on a connected
 * account -- and for everything else we import public metadata and say plainly
 * that the file cannot be fetched. The alternative is scraping or stream
 * ripping, which is what "never bypass platform protections" rules out.
 *
 * Nothing here downloads anything. It decides what is permissible; the worker
 * carries it out.
 */

export type IngestPlatform =
  | "youtube"
  | "instagram"
  | "tiktok"
  | "facebook";

/** How the media can legitimately be obtained, if at all. */
export type MediaAccess =
  /** Official API returns the file, but only for the owner's connected account. */
  | "owner_connected_account"
  /** No official route to the file for anyone. Metadata only. */
  | "metadata_only";

export interface PlatformIngest {
  platform: IngestPlatform;
  label: string;
  mediaAccess: MediaAccess;
  /** Public metadata obtainable without the user connecting an account. */
  publicMetadata: boolean;
  /** Shown when the media itself cannot be fetched. */
  limitation: string;
}

export const INGEST_PLATFORMS: Record<IngestPlatform, PlatformIngest> = {
  youtube: {
    platform: "youtube",
    label: "YouTube",
    mediaAccess: "metadata_only",
    publicMetadata: true,
    limitation:
      "YouTube's API provides metadata but never the video file, and " +
      "downloading it another way breaks their Terms of Service. Title, " +
      "thumbnail and stats are imported; upload the file yourself to process it.",
  },
  instagram: {
    platform: "instagram",
    label: "Instagram",
    mediaAccess: "owner_connected_account",
    publicMetadata: false,
    limitation:
      "Instagram returns media only for a Business or Creator account you " +
      "have connected, and only for that account's own posts. Connect the " +
      "account that published this Reel to import it.",
  },
  tiktok: {
    platform: "tiktok",
    label: "TikTok",
    // The Display API's Video object is metadata only: id, create_time,
    // cover_image_url, share_url, video_description, duration, height, width,
    // title, embed_html, embed_link, the four counts and is_aigc. There is no
    // field carrying the file, for the owner or anyone else. This said
    // "owner_connected_account" and so promised an import that could never
    // happen, whatever the user connected.
    mediaAccess: "metadata_only",
    publicMetadata: false,
    limitation:
      "TikTok's API returns video metadata and an embed link, never the file " +
      "itself -- not even to the account that posted it. Upload the file " +
      "yourself to process it.",
  },
  facebook: {
    platform: "facebook",
    label: "Facebook",
    mediaAccess: "owner_connected_account",
    publicMetadata: false,
    limitation:
      "Facebook returns video only for Pages you have connected and manage. " +
      "Connect the Page that posted this video to import it.",
  },
};

export class UnsupportedUrlError extends Error {
  readonly status = 400;
  constructor(message: string) {
    super(message);
    this.name = "UnsupportedUrlError";
  }
}

export interface ParsedVideoUrl {
  platform: IngestPlatform;
  /** The platform's own id for the video, where the URL carries one. */
  videoId: string;
  /** Normalised https URL, tracking parameters removed. */
  canonicalUrl: string;
  ingest: PlatformIngest;
}

/**
 * Hosts we recognise, longest-suffix first so `m.youtube.com` and
 * `youtube.com` both resolve without one shadowing the other.
 */
const HOST_PLATFORMS: [RegExp, IngestPlatform][] = [
  [/(^|\.)youtube\.com$/i, "youtube"],
  [/(^|\.)youtube-nocookie\.com$/i, "youtube"],
  [/^youtu\.be$/i, "youtube"],
  [/(^|\.)instagram\.com$/i, "instagram"],
  [/(^|\.)tiktok\.com$/i, "tiktok"],
  [/(^|\.)facebook\.com$/i, "facebook"],
  [/^fb\.watch$/i, "facebook"],
];

/** Path shapes that carry a video id, per platform. */
const ID_PATTERNS: Record<IngestPlatform, RegExp[]> = {
  youtube: [
    /^\/watch$/,                       // id comes from ?v=
    /^\/shorts\/([\w-]{5,})/,
    /^\/embed\/([\w-]{5,})/,
    /^\/live\/([\w-]{5,})/,
    /^\/v\/([\w-]{5,})/,
    /^\/([\w-]{5,})$/,                 // youtu.be/<id>
  ],
  instagram: [
    /^\/reels?\/([\w-]+)/,
    /^\/p\/([\w-]+)/,
    /^\/tv\/([\w-]+)/,
    /^\/[\w.]+\/reels?\/([\w-]+)/,
  ],
  tiktok: [
    /^\/@[\w.\-]+\/video\/(\d+)/,
    /^\/v\/(\d+)/,
    /^\/t\/([\w-]+)/,
    /^\/([\w-]{5,})$/,                 // vm.tiktok.com short links
  ],
  facebook: [
    /^\/[\w.]+\/videos\/(\d+)/,
    /^\/reel\/(\d+)/,
    /^\/watch\/?$/,                    // id comes from ?v=
    /^\/videos\/(\d+)/,
    /^\/([\w-]{5,})$/,                 // fb.watch/<code>
  ],
};

function platformForHost(hostname: string): IngestPlatform | null {
  const host = hostname.replace(/^www\./i, "");
  for (const [pattern, platform] of HOST_PLATFORMS) {
    if (pattern.test(host)) return platform;
  }
  return null;
}

/**
 * Detect the platform from a URL, or null if it is not one we support.
 *
 * Separate from `parseVideoUrl` so the UI can say "TikTok" the moment someone
 * pastes, before deciding whether the link points at an actual video.
 */
export function detectPlatform(input: string): IngestPlatform | null {
  const url = safeUrl(input);
  return url ? platformForHost(url.hostname) : null;
}

function safeUrl(input: string): URL | null {
  const raw = String(input ?? "").trim();
  if (!raw) return null;
  // Accept a bare host: people paste "tiktok.com/@a/video/1" without a scheme.
  const withScheme = /^[a-z][a-z0-9+.\-]*:\/\//i.test(raw) ? raw : `https://${raw}`;
  let url: URL;
  try {
    url = new URL(withScheme);
  } catch {
    return null;
  }
  // Only web URLs. `javascript:` and `data:` must never reach the worker.
  if (url.protocol !== "https:" && url.protocol !== "http:") return null;
  return url;
}

/**
 * Parse a pasted link into a platform and video id.
 *
 * Throws UnsupportedUrlError with a message meant for the person who pasted
 * it, because this is the one place the product says no to a user's input and
 * a bare "invalid URL" would leave them guessing.
 */
export function parseVideoUrl(input: string): ParsedVideoUrl {
  const url = safeUrl(input);
  if (!url) {
    throw new UnsupportedUrlError(
      "That does not look like a link. Paste the address of a video post.",
    );
  }

  const platform = platformForHost(url.hostname);
  if (!platform) {
    throw new UnsupportedUrlError(
      `${url.hostname} is not a supported platform. Supported: ` +
        Object.values(INGEST_PLATFORMS).map((p) => p.label).join(", ") + ".",
    );
  }

  const path = url.pathname.replace(/\/+$/, "") || "/";
  let videoId = "";
  let matched = false;

  for (const pattern of ID_PATTERNS[platform]) {
    const match = path.match(pattern);
    if (match) {
      matched = true;
      videoId = match[1] ?? "";
      break;
    }
  }

  // /watch shapes carry the id in the query string instead of the path.
  if (matched && !videoId) {
    videoId = url.searchParams.get("v") ?? "";
  }

  if (!matched || !videoId) {
    throw new UnsupportedUrlError(
      `That ${INGEST_PLATFORMS[platform].label} link does not point at a ` +
        `single video. Open the post itself and copy its address.`,
    );
  }

  return {
    platform,
    videoId,
    canonicalUrl: canonicalise(platform, videoId, url),
    ingest: INGEST_PLATFORMS[platform],
  };
}

/**
 * A stable URL for the video, without tracking parameters.
 *
 * Stored rather than the pasted string so the same video added twice from a
 * share sheet and from a browser is recognisably the same video.
 */
function canonicalise(
  platform: IngestPlatform,
  videoId: string,
  url: URL,
): string {
  switch (platform) {
    case "youtube":
      return `https://www.youtube.com/watch?v=${videoId}`;
    case "instagram":
      return `https://www.instagram.com/reel/${videoId}/`;
    case "tiktok":
      // The canonical form needs the author handle, which short links lack.
      return /^\d+$/.test(videoId) && url.pathname.includes("/@")
        ? `https://www.tiktok.com${url.pathname.replace(/\/+$/, "")}`
        : `https://www.tiktok.com/v/${videoId}`;
    case "facebook":
      return `https://www.facebook.com/watch/?v=${videoId}`;
  }
}

/**
 * Whether the media file can be imported for this link.
 *
 * `connectedPlatforms` is what the user has actually authorised. A platform
 * that returns media only for its owner is importable only when that account
 * is connected -- and even then the worker verifies the video belongs to it,
 * because a connected account is not a claim of ownership over every video on
 * that platform.
 */
export function mediaImportPlan(
  parsed: ParsedVideoUrl,
  connectedPlatforms: Iterable<string>,
): { canFetchMedia: boolean; reason: string } {
  const connected = new Set(connectedPlatforms);
  const { ingest } = parsed;

  if (ingest.mediaAccess === "metadata_only") {
    return { canFetchMedia: false, reason: ingest.limitation };
  }
  if (!connected.has(parsed.platform)) {
    return { canFetchMedia: false, reason: ingest.limitation };
  }
  return {
    canFetchMedia: true,
    reason:
      `Importing from your connected ${ingest.label} account. If this video ` +
      `does not belong to that account, the import will stop.`,
  };
}

/** Every platform a link can be pasted for, for the UI to list. */
export const SUPPORTED_INGEST = Object.values(INGEST_PLATFORMS);

/**
 * The authorized media providers, and what each will actually hand over.
 *
 * "Authorized" means the provider's own API returns the file under terms that
 * permit reuse. That is a short list, and it is short for a reason: the
 * platforms where viral videos live do not license other people's uploads to
 * third parties, and the ways around that -- stream ripping, scraping,
 * unofficial endpoints -- are exactly what this product does not do.
 *
 * Re Create resolves against this list. When nothing here can supply the file,
 * the answer is "unavailable", not a video assembled from something else: a
 * new version built from substitute footage is not a version of the video the
 * user picked, and presenting it as one would be the real failure.
 */
export interface MediaProvider {
  id: string;
  label: string;
  /** What it can supply, in terms a user can act on. */
  supplies: string;
  /** What has to be true before it will. */
  requires: string;
}

export const MEDIA_PROVIDERS: MediaProvider[] = [
  {
    id: "connected_account",
    label: "Your connected account",
    supplies:
      "The original file for videos published by an Instagram or Facebook " +
      "account you have connected. TikTok and YouTube return metadata only.",
    requires: "That account connected, and the video belongs to it.",
  },
  {
    id: "pexels",
    label: "Pexels",
    supplies:
      "Licensed stock photos and short clips, used by the visual engine to " +
      "illustrate scenes.",
    requires: "PEXELS_API_KEY on the worker.",
  },
];

/**
 * Can Re Create obtain this video's file, and if not, what should the user be
 * told?
 *
 * Deliberately returns the same shape whether the answer is yes or no, so a
 * caller cannot forget to handle the no.
 */
export function acquisitionPlan(
  parsed: ParsedVideoUrl,
  connectedPlatforms: Iterable<string>,
): {
  available: boolean;
  provider: string;
  message: string;
} {
  const plan = mediaImportPlan(parsed, connectedPlatforms);
  if (plan.canFetchMedia) {
    return {
      available: true,
      provider: "connected_account",
      message: plan.reason,
    };
  }
  return {
    available: false,
    provider: "",
    // The platform's own limitation is the most useful thing we can say: it
    // names what is missing and, where one exists, the way to fix it.
    message: plan.reason,
  };
}
