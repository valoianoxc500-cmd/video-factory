/**
 * Publishing a carousel to each platform, for real.
 *
 * One interface, four genuinely different implementations — because the
 * platforms are genuinely different, and pretending otherwise is how a
 * carousel gets posted as a single cropped image:
 *
 *   Instagram  N IMAGE containers (is_carousel_item), one CAROUSEL parent,
 *              then publish the parent
 *   Facebook   N unpublished /photos, then one /feed post carrying them all
 *              in attached_media
 *   Threads    N IMAGE items, one CAROUSEL container, then publish it
 *   X          media is uploaded as bytes, not fetched by URL, and at most
 *              four images attach to one post
 *
 * Three of the four ingest images **by URL**, which is why the slides are
 * hosted first and handed over as signed links.
 *
 * Nothing here reports success until the provider returns an id. A publish
 * that "probably worked" is the one outcome this file must never produce,
 * because the user's next action on seeing a tick is to not post it again.
 *
 * Tokens arrive as arguments and are never logged, never returned, and never
 * placed in an error message.
 */

export type PublishPlatform = "instagram" | "facebook" | "threads" | "x";

export interface PublishInput {
  /** Signed, short-lived image URLs in slide order. */
  imageUrls: string[];
  /** Raw PNG bytes, in slide order. Only X needs these. */
  imageBytes?: Uint8Array[];
  caption: string;
  accessToken: string;
  /** Instagram user id, Facebook Page id, Threads user id. */
  accountRef: string;
}

export interface PublishOutcome {
  ok: boolean;
  remotePostId: string;
  /** A category, never a provider message. */
  errorCode: string;
}

export interface CarouselProvider {
  platform: PublishPlatform;
  maxImages: number;
  /** True when this provider can post a true multi-image carousel. */
  carousel: boolean;
  publish(input: PublishInput, fetchImpl?: typeof fetch): Promise<PublishOutcome>;
}

const GRAPH = "https://graph.facebook.com/v21.0";
//: Threads publishes on graph.threads.net while its OAuth lives on
//: graph.threads.com. That split is Meta's, not a typo: the .com hosts are
//: the newer auth endpoints and the API host is still documented as .net.
const THREADS = "https://graph.threads.net/v1.0";
const X_API = "https://api.x.com";
const REQUEST_TIMEOUT_MS = 30_000;

/**
 * Provider faults, reduced to something safe to store and show.
 *
 * The provider's own words never survive this function. Meta error payloads
 * routinely echo the request back, which can include a signed URL; storing
 * that in an audit table would persist a credential-bearing link.
 */
export function classifyError(status: number, body: string): string {
  const text = String(body ?? "").toLowerCase();
  if (status === 401 || status === 403 || /oauth|token|expired|permission/.test(text)) {
    return "auth_expired";
  }
  if (status === 429 || /rate.?limit|too many/.test(text)) return "rate_limited";
  if (/media|image|aspect|resolution|download|fetch/.test(text)) return "media_rejected";
  if (status >= 500) return "provider_unavailable";
  if (status === 400) return "rejected";
  return "failed";
}

/** The one sentence a customer sees for a given category. */
export function customerMessage(errorCode: string, platform: string): string {
  switch (errorCode) {
    case "auth_expired":
      return `Reconnect your ${platform} account and try again.`;
    case "rate_limited":
      return `${platform} is rate limiting posts right now. Try again shortly.`;
    case "media_rejected":
      return `${platform} would not accept these images.`;
    case "provider_unavailable":
      return `${platform} is unavailable right now. Try again shortly.`;
    case "not_connected":
      return `Connect your ${platform} account first.`;
    case "not_configured":
      return `Publishing to ${platform} is not available yet.`;
    case "too_many_slides":
      return `This carousel has more slides than ${platform} accepts.`;
    default:
      return `Could not publish to ${platform}.`;
  }
}

interface ApiResult {
  ok: boolean;
  status: number;
  body: Record<string, unknown>;
  raw: string;
}

async function call(
  url: string,
  init: RequestInit,
  fetchImpl: typeof fetch,
): Promise<ApiResult> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  try {
    const response = await fetchImpl(url, { ...init, signal: controller.signal });
    const raw = await response.text();
    let body: Record<string, unknown> = {};
    try {
      body = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
    } catch {
      body = {};
    }
    return { ok: response.ok, status: response.status, body, raw };
  } catch (error) {
    const aborted = (error as Error)?.name === "AbortError";
    return {
      ok: false,
      status: aborted ? 504 : 502,
      body: {},
      raw: aborted ? "timeout" : "network",
    };
  } finally {
    clearTimeout(timer);
  }
}

function form(fields: Record<string, string>): RequestInit {
  return {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(fields).toString(),
  };
}

const failure = (errorCode: string): PublishOutcome => ({
  ok: false,
  remotePostId: "",
  errorCode,
});

// ── Instagram ────────────────────────────────────────────────────────

export const instagramProvider: CarouselProvider = {
  platform: "instagram",
  maxImages: 10,
  carousel: true,

  async publish(input, fetchImpl = fetch) {
    const slides = input.imageUrls.slice(0, this.maxImages);
    if (!slides.length) return failure("media_rejected");

    // 1. One container per slide, flagged as a carousel item.
    const children: string[] = [];
    for (const imageUrl of slides) {
      const result = await call(
        `${GRAPH}/${input.accountRef}/media`,
        form({
          image_url: imageUrl,
          is_carousel_item: "true",
          access_token: input.accessToken,
        }),
        fetchImpl,
      );
      if (!result.ok || !result.body.id) {
        return failure(classifyError(result.status, result.raw));
      }
      children.push(String(result.body.id));
    }

    // 2. The parent, which is where the order is fixed.
    const parent = await call(
      `${GRAPH}/${input.accountRef}/media`,
      form({
        media_type: "CAROUSEL",
        children: children.join(","),
        caption: input.caption,
        access_token: input.accessToken,
      }),
      fetchImpl,
    );
    if (!parent.ok || !parent.body.id) {
      return failure(classifyError(parent.status, parent.raw));
    }

    // 3. Publishing the parent is what makes it a post. Until this returns
    //    an id, nothing is live and nothing may be reported as published.
    const published = await call(
      `${GRAPH}/${input.accountRef}/media_publish`,
      form({
        creation_id: String(parent.body.id),
        access_token: input.accessToken,
      }),
      fetchImpl,
    );
    if (!published.ok || !published.body.id) {
      return failure(classifyError(published.status, published.raw));
    }
    return { ok: true, remotePostId: String(published.body.id), errorCode: "" };
  },
};

// ── Facebook ─────────────────────────────────────────────────────────

export const facebookProvider: CarouselProvider = {
  platform: "facebook",
  maxImages: 10,
  carousel: true,

  async publish(input, fetchImpl = fetch) {
    const slides = input.imageUrls.slice(0, this.maxImages);
    if (!slides.length) return failure("media_rejected");

    // 1. Upload each photo unpublished, so none of them appears alone.
    const attached: { media_fbid: string }[] = [];
    for (const imageUrl of slides) {
      const result = await call(
        `${GRAPH}/${input.accountRef}/photos`,
        form({
          url: imageUrl,
          published: "false",
          access_token: input.accessToken,
        }),
        fetchImpl,
      );
      if (!result.ok || !result.body.id) {
        return failure(classifyError(result.status, result.raw));
      }
      attached.push({ media_fbid: String(result.body.id) });
    }

    // 2. One feed post carrying all of them, in order.
    const post = await call(
      `${GRAPH}/${input.accountRef}/feed`,
      form({
        message: input.caption,
        attached_media: JSON.stringify(attached),
        access_token: input.accessToken,
      }),
      fetchImpl,
    );
    if (!post.ok || !post.body.id) {
      return failure(classifyError(post.status, post.raw));
    }
    return { ok: true, remotePostId: String(post.body.id), errorCode: "" };
  },
};

// ── Threads ──────────────────────────────────────────────────────────

export const threadsProvider: CarouselProvider = {
  platform: "threads",
  maxImages: 20,
  carousel: true,

  async publish(input, fetchImpl = fetch) {
    const slides = input.imageUrls.slice(0, this.maxImages);
    if (!slides.length) return failure("media_rejected");

    // 1. An item container per slide. Threads uses its own host and its own
    //    token; it is not the Instagram Graph API with a different path.
    const children: string[] = [];
    for (const imageUrl of slides) {
      const result = await call(
        `${THREADS}/${input.accountRef}/threads`,
        form({
          media_type: "IMAGE",
          image_url: imageUrl,
          is_carousel_item: "true",
          access_token: input.accessToken,
        }),
        fetchImpl,
      );
      if (!result.ok || !result.body.id) {
        return failure(classifyError(result.status, result.raw));
      }
      children.push(String(result.body.id));
    }

    const parent = await call(
      `${THREADS}/${input.accountRef}/threads`,
      form({
        media_type: "CAROUSEL",
        children: children.join(","),
        text: input.caption,
        access_token: input.accessToken,
      }),
      fetchImpl,
    );
    if (!parent.ok || !parent.body.id) {
      return failure(classifyError(parent.status, parent.raw));
    }

    const published = await call(
      `${THREADS}/${input.accountRef}/threads_publish`,
      form({
        creation_id: String(parent.body.id),
        access_token: input.accessToken,
      }),
      fetchImpl,
    );
    if (!published.ok || !published.body.id) {
      return failure(classifyError(published.status, published.raw));
    }
    return { ok: true, remotePostId: String(published.body.id), errorCode: "" };
  },
};

// ── X ────────────────────────────────────────────────────────────────

export const xProvider: CarouselProvider = {
  platform: "x",
  maxImages: 4,
  carousel: false,

  async publish(input, fetchImpl = fetch) {
    const bytes = input.imageBytes ?? [];
    if (!bytes.length) return failure("media_rejected");

    // X has no carousel and does not fetch by URL. More slides than it can
    // carry is refused here rather than silently truncated -- the caller is
    // responsible for having asked the user what to do about it, and a
    // provider that quietly drops slides makes that question meaningless.
    if (bytes.length > this.maxImages) return failure("too_many_slides");

    const mediaIds: string[] = [];
    for (const image of bytes) {
      const body = new FormData();
      body.append("media", new Blob([image as BlobPart], { type: "image/png" }));
      const result = await call(
        // v2. The old upload.twitter.com/1.1 host returned `media_id_string`;
        // v2 returns `id`, so both are read and the v2 name wins.
        `${X_API}/2/media/upload`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${input.accessToken}` },
          body,
        },
        fetchImpl,
      );
      const data = (result.body.data ?? result.body) as Record<string, unknown>;
      const id = String(data.id ?? result.body.media_id_string ?? "");
      if (!result.ok || !id) {
        return failure(classifyError(result.status, result.raw));
      }
      mediaIds.push(id);
    }

    const post = await call(
      `${X_API}/2/tweets`,
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${input.accessToken}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          text: input.caption,
          media: { media_ids: mediaIds },
        }),
      },
      fetchImpl,
    );
    const data = (post.body.data ?? {}) as Record<string, unknown>;
    if (!post.ok || !data.id) {
      return failure(classifyError(post.status, post.raw));
    }
    return { ok: true, remotePostId: String(data.id), errorCode: "" };
  },
};

export const PROVIDERS: Record<PublishPlatform, CarouselProvider> = {
  instagram: instagramProvider,
  facebook: facebookProvider,
  threads: threadsProvider,
  x: xProvider,
};

export function providerFor(platform: string): CarouselProvider | null {
  return PROVIDERS[platform as PublishPlatform] ?? null;
}
