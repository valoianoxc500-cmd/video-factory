"use strict";

/**
 * Publish validation and planning in the browser-facing layer.
 *
 * The worker re-checks all of this before it posts. These tests cover the
 * earlier gate: telling the user about an unsupported platform or an
 * impossible schedule while they are still looking at the form.
 */

const test = require("node:test");
const assert = require("node:assert");

const {
  captionFor,
  planPublish,
  validatePublish,
  CAPTION_LIMITS,
} = require("../.test-build/vrf-publish.js");

const NOW = new Date("2026-09-06T12:00:00Z");

function input(over = {}) {
  return {
    assetId: "asset-1",
    caption: "A caption",
    platforms: ["youtube"],
    scheduledFor: null,
    attribution: "",
    ...over,
  };
}

// ── validation ──────────────────────────────────────────────────────────────

test("a valid request has no problems", () => {
  assert.deepStrictEqual(validatePublish(input(), NOW), []);
});

test("no platform selected is refused", () => {
  assert.ok(validatePublish(input({ platforms: [] }), NOW).length > 0);
});

test("no video selected is refused", () => {
  assert.ok(validatePublish(input({ assetId: "" }), NOW).length > 0);
});

test("snapchat is refused up front", () => {
  const problems = validatePublish(input({ platforms: ["snapchat"] }), NOW);
  assert.ok(problems.some((p) => p.toLowerCase().includes("snapchat")));
});

test("an unknown platform is refused", () => {
  const problems = validatePublish(input({ platforms: ["myspace"] }), NOW);
  assert.ok(problems.some((p) => p.includes("myspace")));
});

test("a past schedule is refused", () => {
  const problems = validatePublish(
    input({ scheduledFor: "2026-09-05T12:00:00Z" }),
    NOW,
  );
  assert.ok(problems.some((p) => p.includes("past")));
});

test("a schedule far in the future is refused", () => {
  const problems = validatePublish(
    input({ scheduledFor: "2028-01-01T12:00:00Z" }),
    NOW,
  );
  assert.ok(problems.some((p) => p.includes("180 days")));
});

test("an unparseable date is refused rather than silently ignored", () => {
  const problems = validatePublish(input({ scheduledFor: "next tuesday" }), NOW);
  assert.ok(problems.some((p) => p.includes("not a valid date")));
});

// ── planning ────────────────────────────────────────────────────────────────

test("one job per platform", () => {
  const jobs = planPublish(input({ platforms: ["youtube", "instagram"] }), NOW);
  assert.deepStrictEqual(
    jobs.map((j) => j.platform).sort(),
    ["instagram", "youtube"],
  );
});

test("native scheduling is used where it exists", () => {
  const jobs = planPublish(
    input({
      platforms: ["youtube", "instagram"],
      scheduledFor: "2026-09-07T12:00:00Z",
    }),
    NOW,
  );
  const byPlatform = Object.fromEntries(jobs.map((j) => [j.platform, j]));
  assert.strictEqual(byPlatform.youtube.mode, "native_schedule");
  assert.strictEqual(byPlatform.instagram.mode, "queue_until_due");
});

test("a natively scheduled job runs now; a queued one waits", () => {
  const jobs = planPublish(
    input({
      platforms: ["youtube", "instagram"],
      scheduledFor: "2026-09-07T12:00:00Z",
    }),
    NOW,
  );
  const byPlatform = Object.fromEntries(jobs.map((j) => [j.platform, j]));
  assert.strictEqual(byPlatform.youtube.next_attempt_at, NOW.toISOString());
  assert.strictEqual(
    byPlatform.instagram.next_attempt_at,
    new Date("2026-09-07T12:00:00Z").toISOString(),
  );
});

test("an unsupported platform is planned as unsupported, not queued", () => {
  const jobs = planPublish(input({ platforms: ["snapchat"] }), NOW);
  assert.strictEqual(jobs[0].status, "unsupported");
  assert.ok(jobs[0].reason);
});

test("an immediate publish has no delivery time", () => {
  const jobs = planPublish(input(), NOW);
  assert.strictEqual(jobs[0].mode, "publish_now");
  assert.strictEqual(jobs[0].deliver_at, null);
});

// ── captions ────────────────────────────────────────────────────────────────

test("attribution is appended", () => {
  const caption = captionFor("Great clip", "Credit: A. Creator", "instagram");
  assert.ok(caption.includes("A. Creator"));
});

test("attribution is not duplicated", () => {
  const caption = captionFor(
    "Credit: A. Creator made this",
    "Credit: A. Creator",
    "instagram",
  );
  assert.strictEqual(caption.split("A. Creator").length - 1, 1);
});

test("captions are trimmed to each platform limit", () => {
  const long = "x".repeat(9000);
  assert.ok(captionFor(long, "", "instagram").length <= CAPTION_LIMITS.instagram);
  assert.ok(captionFor(long, "", "youtube").length <= CAPTION_LIMITS.youtube);
});

test("an unknown platform falls back to the tightest sensible limit", () => {
  assert.ok(captionFor("x".repeat(9000), "", "myspace").length <= 2200);
});
