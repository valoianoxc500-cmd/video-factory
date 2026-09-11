/**
 * Carousel publishing: order, payloads, and never lying about success.
 *
 * Every provider call is mocked — no live posting, no paid call, no token.
 * What is being pinned is the part that cannot be checked by reading: the
 * exact sequence of API calls each platform needs, that slide order survives
 * it, and that nothing reports "published" without an id from the provider.
 *
 * Run with: npm run test  (compiles lib/ first -- see package.json)
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const providers = require("../.test-build/social/carousel-providers.js");
const storage = require("../.test-build/carousel-storage.js");

const USER = "11111111-2222-4333-8444-555555555555";
const PROJECT = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const PUBLISH = "99999999-8888-4777-8666-555555555555";

const URLS = [
  "https://storage.googleapis.com/b/quotes/a/slide-01.png?sig=1",
  "https://storage.googleapis.com/b/quotes/a/slide-02.png?sig=2",
  "https://storage.googleapis.com/b/quotes/a/slide-03.png?sig=3",
];

const TOKEN = "super-secret-access-token";

/** A fetch that answers each call in turn and records what it was sent. */
function stub(responses) {
  const calls = [];
  let i = 0;
  const impl = async (url, init) => {
    const body = init?.body;
    // Meta and Threads send urlencoded forms; X sends JSON; media upload
    // sends FormData. Parsing a JSON body as urlencoded silently yields
    // nonsense rather than throwing, which is what made this harness lie.
    let fields = {};
    if (typeof body === "string") {
      const trimmed = body.trim();
      fields =
        trimmed.startsWith("{") || trimmed.startsWith("[")
          ? JSON.parse(trimmed)
          : Object.fromEntries(new URLSearchParams(body));
    }
    calls.push({ url: String(url), fields, headers: init?.headers ?? {} });
    const next = responses[Math.min(i++, responses.length - 1)];
    return {
      ok: next.ok !== false,
      status: next.status ?? 200,
      text: async () => JSON.stringify(next.body ?? {}),
    };
  };
  impl.calls = calls;
  return impl;
}

const okId = (id) => ({ body: { id } });

// ── object paths carry the order ─────────────────────────────────────

test("slide paths are zero-padded so lexical order is slide order", () => {
  const paths = [0, 1, 9, 10].map((i) =>
    storage.slideObjectPath(USER, PROJECT, PUBLISH, i),
  );
  assert.ok(paths[0].endsWith("slide-01.png"));
  assert.ok(paths[2].endsWith("slide-10.png"));
  assert.ok(paths[3].endsWith("slide-11.png"));
  // Sorting the strings must give the same order as sorting the indices.
  assert.deepEqual([...paths].sort(), paths);
});

test("the slide index is recoverable from the object name", () => {
  const path = storage.slideObjectPath(USER, PROJECT, PUBLISH, 4);
  assert.equal(storage.slideIndexOf(path), 4);
});

test("a carousel larger than any platform accepts is refused", () => {
  assert.throws(
    () => storage.slideObjectPath(USER, PROJECT, PUBLISH, 25),
    /too many slides/i,
  );
});

test("hostile slide paths are refused", () => {
  const hostile = [
    "",
    "/quotes/a/b/c/slide-01.png",
    `quotes/${USER}/${PROJECT}/${PUBLISH}/../../secret.png`,
    `quotes/${USER}/${PROJECT}/${PUBLISH}/slide-01.png.exe`,
    `vrf/uploads/${USER}/${PROJECT}/slide-01.png`,
    `quotes/${USER}/${PROJECT}/${PUBLISH}/slide-1.png`,
    "videos/horror/a/b.mp4",
  ];
  for (const path of hostile) {
    assert.throws(() => storage.assertSlidePath(path), `accepted: ${path}`);
  }
});

test("a Clipping object cannot be signed by the carousel signer", () => {
  // Each product guards its own prefix; neither may sign the other's.
  assert.throws(() =>
    storage.assertSlidePath(`vrf/uploads/${USER}/${PROJECT}/clip.mp4`),
  );
});

// ── Instagram ────────────────────────────────────────────────────────

test("instagram builds children, then a carousel parent, then publishes", async () => {
  const fetchImpl = stub([
    okId("c1"), okId("c2"), okId("c3"), // children
    okId("parent"),                      // CAROUSEL container
    okId("post-1"),                      // media_publish
  ]);
  const out = await providers.instagramProvider.publish(
    { imageUrls: URLS, caption: "hello", accessToken: TOKEN, accountRef: "ig-1" },
    fetchImpl,
  );

  assert.equal(out.ok, true);
  assert.equal(out.remotePostId, "post-1");
  assert.equal(fetchImpl.calls.length, 5);

  // Children are created in slide order, and each is flagged a carousel item.
  assert.deepEqual(
    fetchImpl.calls.slice(0, 3).map((c) => c.fields.image_url),
    URLS,
  );
  assert.ok(fetchImpl.calls.slice(0, 3).every((c) => c.fields.is_carousel_item === "true"));

  // The parent fixes the order and carries the caption.
  assert.equal(fetchImpl.calls[3].fields.media_type, "CAROUSEL");
  assert.equal(fetchImpl.calls[3].fields.children, "c1,c2,c3");
  assert.equal(fetchImpl.calls[3].fields.caption, "hello");

  assert.match(fetchImpl.calls[4].url, /media_publish$/);
  assert.equal(fetchImpl.calls[4].fields.creation_id, "parent");
});

test("instagram is not published until media_publish returns an id", async () => {
  const fetchImpl = stub([
    okId("c1"), okId("c2"), okId("c3"), okId("parent"),
    { ok: false, status: 400, body: { error: { message: "bad" } } },
  ]);
  const out = await providers.instagramProvider.publish(
    { imageUrls: URLS, caption: "x", accessToken: TOKEN, accountRef: "ig-1" },
    fetchImpl,
  );
  // A container existing is not a post existing.
  assert.equal(out.ok, false);
  assert.equal(out.remotePostId, "");
});

test("instagram stops at the first failed child", async () => {
  const fetchImpl = stub([okId("c1"), { ok: false, status: 400, body: {} }]);
  const out = await providers.instagramProvider.publish(
    { imageUrls: URLS, caption: "x", accessToken: TOKEN, accountRef: "ig-1" },
    fetchImpl,
  );
  assert.equal(out.ok, false);
  assert.equal(fetchImpl.calls.length, 2, "it kept going after a failure");
});

test("instagram caps at ten slides", () => {
  assert.equal(providers.instagramProvider.maxImages, 10);
});

// ── Facebook ─────────────────────────────────────────────────────────

test("facebook uploads unpublished photos then attaches them in order", async () => {
  const fetchImpl = stub([okId("p1"), okId("p2"), okId("p3"), okId("post-2")]);
  const out = await providers.facebookProvider.publish(
    { imageUrls: URLS, caption: "caption", accessToken: TOKEN, accountRef: "page-1" },
    fetchImpl,
  );

  assert.equal(out.ok, true);
  assert.equal(out.remotePostId, "post-2");

  // Each photo is uploaded unpublished, so none appears on its own.
  assert.ok(fetchImpl.calls.slice(0, 3).every((c) => c.fields.published === "false"));
  assert.ok(fetchImpl.calls.slice(0, 3).every((c) => /\/photos$/.test(c.url)));

  const attached = JSON.parse(fetchImpl.calls[3].fields.attached_media);
  assert.deepEqual(attached, [
    { media_fbid: "p1" },
    { media_fbid: "p2" },
    { media_fbid: "p3" },
  ]);
  assert.match(fetchImpl.calls[3].url, /\/feed$/);
  assert.equal(fetchImpl.calls[3].fields.message, "caption");
});

test("facebook does not use the video endpoint", async () => {
  const fetchImpl = stub([okId("p1"), okId("p2"), okId("p3"), okId("post")]);
  await providers.facebookProvider.publish(
    { imageUrls: URLS, caption: "x", accessToken: TOKEN, accountRef: "page-1" },
    fetchImpl,
  );
  assert.ok(fetchImpl.calls.every((c) => !/\/videos/.test(c.url)));
});

// ── Threads ──────────────────────────────────────────────────────────

test("threads builds a carousel on its own host", async () => {
  const fetchImpl = stub([okId("t1"), okId("t2"), okId("t3"), okId("tp"), okId("post-3")]);
  const out = await providers.threadsProvider.publish(
    { imageUrls: URLS, caption: "text here", accessToken: TOKEN, accountRef: "th-1" },
    fetchImpl,
  );

  assert.equal(out.ok, true);
  assert.equal(out.remotePostId, "post-3");
  // Threads is not the Instagram Graph API with a different path.
  assert.ok(fetchImpl.calls.every((c) => c.url.includes("graph.threads.net")));
  assert.equal(fetchImpl.calls[3].fields.media_type, "CAROUSEL");
  assert.equal(fetchImpl.calls[3].fields.children, "t1,t2,t3");
  assert.equal(fetchImpl.calls[3].fields.text, "text here");
  assert.match(fetchImpl.calls[4].url, /threads_publish$/);
});

// ── X ────────────────────────────────────────────────────────────────

test("x refuses to silently truncate a long carousel", async () => {
  const fetchImpl = stub([okId("never")]);
  const out = await providers.xProvider.publish(
    {
      imageUrls: [],
      imageBytes: Array.from({ length: 6 }, () => new Uint8Array([1])),
      caption: "x",
      accessToken: TOKEN,
      accountRef: "x-1",
    },
    fetchImpl,
  );
  assert.equal(out.ok, false);
  assert.equal(out.errorCode, "too_many_slides");
  assert.equal(fetchImpl.calls.length, 0, "it contacted X anyway");
});

test("x posts up to four images on one post", async () => {
  const fetchImpl = stub([
    { body: { media_id_string: "m1" } },
    { body: { media_id_string: "m2" } },
    { body: { data: { id: "tweet-1" } } },
  ]);
  const out = await providers.xProvider.publish(
    {
      imageUrls: [],
      imageBytes: [new Uint8Array([1]), new Uint8Array([2])],
      caption: "short",
      accessToken: TOKEN,
      accountRef: "x-1",
    },
    fetchImpl,
  );
  assert.equal(out.ok, true);
  assert.equal(out.remotePostId, "tweet-1");
  assert.deepEqual(fetchImpl.calls[2].fields.media.media_ids, ["m1", "m2"]);
});

test("x is not a carousel platform", () => {
  assert.equal(providers.xProvider.carousel, false);
  assert.equal(providers.xProvider.maxImages, 4);
});

// ── error handling never leaks ───────────────────────────────────────

test("provider faults become categories, never provider text", async () => {
  const cases = [
    [401, "OAuthException: token expired for user 123", "auth_expired"],
    [429, "Application request limit reached", "rate_limited"],
    [400, "The image could not be downloaded", "media_rejected"],
    [503, "internal", "provider_unavailable"],
  ];
  for (const [status, text, expected] of cases) {
    assert.equal(providers.classifyError(status, text), expected);
  }
});

test("an error category never carries a token or a signed url", async () => {
  const leaky = `Bearer ${TOKEN} failed fetching ${URLS[0]}`;
  const code = providers.classifyError(400, leaky);
  assert.ok(!code.includes(TOKEN));
  assert.ok(!code.includes("storage.googleapis.com"));

  const message = providers.customerMessage(code, "Instagram");
  assert.ok(!message.includes(TOKEN));
  assert.ok(!message.includes("http"));
});

test("every customer message is a plain sentence", () => {
  const codes = [
    "auth_expired", "rate_limited", "media_rejected", "provider_unavailable",
    "not_connected", "not_configured", "too_many_slides", "anything_else",
  ];
  for (const code of codes) {
    const message = providers.customerMessage(code, "Instagram");
    assert.ok(message.length > 0 && message.length < 120, code);
    assert.ok(!/oauth|graph\.|token|http|json/i.test(message), `leaked: ${message}`);
  }
});

test("a network failure is a failure, not a success", async () => {
  const fetchImpl = async () => {
    throw new TypeError("fetch failed");
  };
  const out = await providers.instagramProvider.publish(
    { imageUrls: URLS, caption: "x", accessToken: TOKEN, accountRef: "ig-1" },
    fetchImpl,
  );
  assert.equal(out.ok, false);
  assert.equal(out.remotePostId, "");
});

test("an empty carousel is refused by every provider", async () => {
  for (const provider of Object.values(providers.PROVIDERS)) {
    const out = await provider.publish(
      { imageUrls: [], imageBytes: [], caption: "x", accessToken: TOKEN, accountRef: "a" },
      async () => {
        throw new Error("should not be called");
      },
    );
    assert.equal(out.ok, false, provider.platform);
  }
});

test("providerFor only resolves the four real platforms", () => {
  for (const platform of ["instagram", "facebook", "threads", "x"]) {
    assert.ok(providers.providerFor(platform));
  }
  assert.equal(providers.providerFor("myspace"), null);
  assert.equal(providers.providerFor(""), null);
});
