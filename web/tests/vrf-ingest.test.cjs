/**
 * URL recognition and what may legitimately be fetched from each platform.
 *
 * Two things are being protected here. The first is ordinary correctness: a
 * link the user pastes must resolve to the right platform and video.
 *
 * The second matters more. `mediaImportPlan` is the decision about whether we
 * may fetch a media file at all, and the honest answer is almost always no --
 * official APIs do not hand out other people's videos. If that ever silently
 * flipped to "yes", the product would be scraping. Several tests below exist
 * purely to make that flip loud.
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const ingest = require("../.test-build/vrf-ingest.js");

// ── platform detection ────────────────────────────────────────────

const RECOGNISED = [
  ["https://www.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"],
  ["https://youtu.be/dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"],
  ["https://www.youtube.com/shorts/abc123XYZ", "youtube", "abc123XYZ"],
  ["https://m.youtube.com/watch?v=dQw4w9WgXcQ", "youtube", "dQw4w9WgXcQ"],
  ["https://www.instagram.com/reel/CxYzAbC123/", "instagram", "CxYzAbC123"],
  ["https://instagram.com/p/CxYzAbC123/", "instagram", "CxYzAbC123"],
  ["https://www.tiktok.com/@someone/video/7212345678901234567", "tiktok", "7212345678901234567"],
  ["https://www.facebook.com/reel/1234567890", "facebook", "1234567890"],
];

for (const [url, platform, videoId] of RECOGNISED) {
  test(`recognises ${platform}: ${url}`, () => {
    const parsed = ingest.parseVideoUrl(url);
    assert.equal(parsed.platform, platform);
    assert.equal(parsed.videoId, videoId);
  });
}

test("a URL without a scheme is still recognised", () => {
  const parsed = ingest.parseVideoUrl("youtube.com/watch?v=dQw4w9WgXcQ");
  assert.equal(parsed.platform, "youtube");
  assert.equal(parsed.videoId, "dQw4w9WgXcQ");
});

test("surrounding whitespace is tolerated", () => {
  const parsed = ingest.parseVideoUrl("  https://youtu.be/dQw4w9WgXcQ  ");
  assert.equal(parsed.videoId, "dQw4w9WgXcQ");
});

test("tracking parameters are dropped from the stored URL", () => {
  const parsed = ingest.parseVideoUrl(
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&utm_source=share&si=xyz",
  );
  assert.equal(parsed.canonicalUrl, "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  assert.ok(!parsed.canonicalUrl.includes("utm_source"));
});

// ── unsupported and malformed input ───────────────────────────────

test("an unsupported platform is refused by name", () => {
  assert.throws(
    () => ingest.parseVideoUrl("https://vimeo.com/123456789"),
    (err) => {
      assert.equal(err.name, "UnsupportedUrlError");
      assert.equal(err.status, 400);
      assert.match(err.message, /vimeo\.com is not a supported platform/i);
      return true;
    },
  );
});

test("the refusal lists what is supported", () => {
  try {
    ingest.parseVideoUrl("https://example.com/video/1");
    assert.fail("should have refused");
  } catch (err) {
    for (const label of ["YouTube", "Instagram", "TikTok", "Facebook"]) {
      assert.match(err.message, new RegExp(label));
    }
  }
});

test("plain text is refused with a usable message", () => {
  assert.throws(
    () => ingest.parseVideoUrl("my holiday video"),
    /does not look like a link|not a supported platform/i,
  );
});

test("an empty URL is refused", () => {
  assert.throws(() => ingest.parseVideoUrl(""), /does not look like a link/i);
  assert.throws(() => ingest.parseVideoUrl("   "), /does not look like a link/i);
});

test("non-http schemes are refused", () => {
  for (const hostile of [
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "file:///etc/passwd",
  ]) {
    assert.throws(
      () => ingest.parseVideoUrl(hostile),
      /does not look like a link|not a supported platform/i,
      `should have refused ${hostile}`,
    );
  }
});

test("a supported host that is not a video link is refused clearly", () => {
  assert.throws(
    () => ingest.parseVideoUrl("https://www.youtube.com/"),
    /does not point at a single video/i,
  );
  assert.throws(
    () => ingest.parseVideoUrl("https://www.instagram.com/someuser/"),
    /does not point at a single video/i,
  );
});

test("a lookalike domain is not mistaken for the real one", () => {
  for (const hostile of [
    "https://youtube.com.evil.example/watch?v=abc12345",
    "https://nottiktok.com/@a/video/123",
    "https://evil-instagram.com/reel/abc",
  ]) {
    assert.throws(
      () => ingest.parseVideoUrl(hostile),
      /not a supported platform/i,
      `${hostile} was treated as a supported platform`,
    );
  }
});

test("detectPlatform reports null rather than throwing", () => {
  assert.equal(ingest.detectPlatform("https://vimeo.com/1"), null);
  assert.equal(ingest.detectPlatform("nonsense"), null);
  assert.equal(ingest.detectPlatform("https://youtu.be/abc12345"), "youtube");
});

// ── what may be fetched ───────────────────────────────────────────

test("YouTube never yields the media file, even when connected", () => {
  const parsed = ingest.parseVideoUrl("https://youtu.be/dQw4w9WgXcQ");
  const plan = ingest.mediaImportPlan(parsed, ["youtube", "tiktok", "instagram"]);
  assert.equal(
    plan.canFetchMedia,
    false,
    "connecting a YouTube account must not unlock downloading arbitrary videos",
  );
  assert.match(plan.reason, /Terms of Service|never the video file/i);
});

test("an owner-only platform yields nothing without a connected account", () => {
  const parsed = ingest.parseVideoUrl("https://www.instagram.com/reel/CxYzAbC123/");
  const plan = ingest.mediaImportPlan(parsed, []);
  assert.equal(plan.canFetchMedia, false);
  assert.match(plan.reason, /connect/i);
});

test("an owner-only platform yields media once its account is connected", () => {
  const parsed = ingest.parseVideoUrl("https://www.instagram.com/reel/CxYzAbC123/");
  const plan = ingest.mediaImportPlan(parsed, ["instagram"]);
  assert.equal(plan.canFetchMedia, true);
  // The connection is not itself a claim of ownership over every video there.
  assert.match(plan.reason, /does not belong to that account/i);
});

test("connecting TikTok does not unlock a file TikTok never returns", () => {
  // Its Display API Video object has no file field, for the owner or anyone
  // else, so a connected account changes nothing here.
  const parsed = ingest.parseVideoUrl(
    "https://www.tiktok.com/@someone/video/7212345678901234567",
  );
  const plan = ingest.mediaImportPlan(parsed, ["tiktok"]);
  assert.equal(plan.canFetchMedia, false);
  assert.match(plan.reason, /never the file/i);
});

test("connecting one platform does not unlock another", () => {
  const parsed = ingest.parseVideoUrl("https://www.instagram.com/reel/CxYzAbC123/");
  assert.equal(ingest.mediaImportPlan(parsed, ["tiktok"]).canFetchMedia, false);
  assert.equal(ingest.mediaImportPlan(parsed, ["instagram"]).canFetchMedia, true);
});

test("every platform states a limitation the user can act on", () => {
  for (const platform of ingest.SUPPORTED_INGEST) {
    assert.ok(platform.limitation.length > 40, `${platform.platform} has no limitation text`);
    assert.ok(platform.label, `${platform.platform} has no label`);
  }
});

// ── Re Create acquisition ─────────────────────────────────────────

test("acquisition succeeds only through an authorized provider", () => {
  const parsed = ingest.parseVideoUrl("https://www.instagram.com/reel/CxYzAbC123/");
  const connected = ingest.acquisitionPlan(parsed, ["instagram"]);
  assert.equal(connected.available, true);
  assert.equal(connected.provider, "connected_account");
});

test("acquisition reports unavailable rather than substituting footage", () => {
  const parsed = ingest.parseVideoUrl("https://youtu.be/dQw4w9WgXcQ");
  const plan = ingest.acquisitionPlan(parsed, ["youtube", "tiktok"]);
  assert.equal(plan.available, false);
  assert.equal(plan.provider, "");
  assert.ok(plan.message.length > 40, "unavailable must explain why");
});

test("an unavailable acquisition never names a stock provider", () => {
  // Building a "new version" out of substitute footage would not be a version
  // of the video the user picked.
  const parsed = ingest.parseVideoUrl("https://youtu.be/dQw4w9WgXcQ");
  const plan = ingest.acquisitionPlan(parsed, []);
  assert.doesNotMatch(plan.provider, /pexels/i);
});

test("every declared provider says what it supplies and requires", () => {
  assert.ok(ingest.MEDIA_PROVIDERS.length >= 2);
  for (const provider of ingest.MEDIA_PROVIDERS) {
    assert.ok(provider.id && provider.label);
    assert.ok(provider.supplies.length > 20, `${provider.id} supplies nothing`);
    assert.ok(provider.requires.length > 10, `${provider.id} requires nothing`);
  }
});

test("no platform claims media access it cannot have", () => {
  // metadata_only means metadata_only: there is no code path that upgrades it.
  const youtube = ingest.INGEST_PLATFORMS.youtube;
  assert.equal(youtube.mediaAccess, "metadata_only");
  const parsed = ingest.parseVideoUrl("https://youtu.be/dQw4w9WgXcQ");
  for (const connected of [[], ["youtube"], ["youtube", "tiktok", "facebook", "instagram"]]) {
    assert.equal(ingest.mediaImportPlan(parsed, connected).canFetchMedia, false);
  }
});
