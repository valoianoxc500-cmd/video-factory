/**
 * A clipping job must never display as progress forever.
 *
 * A real production task sat `queued` for eighteen hours with `attempts = 0`
 * and `started_at = null` -- no worker had ever claimed it -- while the screen
 * showed "Analyzing hooks, moments, speakers, and pacing." Nothing was
 * analysing anything, and nothing would ever change the status, so the
 * spinner had no end condition.
 *
 * This pins the rule that replaced it. The logic is duplicated here rather
 * than imported because it lives inside a client component; the duplication
 * is deliberate and the constant is asserted against the component's own
 * source so the two cannot drift.
 *
 * Run with: npm run test  (compiles lib/ first -- see package.json)
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "components", "reels", "ViralClipping.tsx"),
  "utf8",
);

const STALE_AFTER_MS = 15 * 60 * 1000;

/** Mirror of the component's `isStale`. */
function isStale(task, now) {
  if (!task || (task.status !== "queued" && task.status !== "running")) return false;
  const queuedAt = Date.parse(String(task.created_at ?? ""));
  if (!Number.isFinite(queuedAt)) return false;
  return now - queuedAt > STALE_AFTER_MS;
}

const at = (minutesAgo) =>
  new Date(Date.now() - minutesAgo * 60_000).toISOString();
const NOW = Date.now();

// ── the rule ─────────────────────────────────────────────────────────

test("a job queued moments ago is not stale", () => {
  assert.equal(isStale({ status: "queued", created_at: at(1) }, NOW), false);
});

test("a job still inside the window is not stale", () => {
  assert.equal(isStale({ status: "running", created_at: at(14) }, NOW), false);
});

test("a job past the window is stale", () => {
  assert.equal(isStale({ status: "queued", created_at: at(16) }, NOW), true);
});

test("the eighteen-hour production job would have been caught", () => {
  // The exact shape of ef4c7092: queued, never claimed, 18 hours old.
  assert.equal(isStale({ status: "queued", created_at: at(18 * 60) }, NOW), true);
});

test("a running job that is genuinely stuck is caught too", () => {
  // A worker that claimed a task and then crashed leaves it `running`
  // forever; that spins just as hard as a never-claimed one.
  assert.equal(isStale({ status: "running", created_at: at(60) }, NOW), true);
});

// ── what must NOT be called stale ────────────────────────────────────

test("a finished job is never stale", () => {
  for (const status of ["done", "failed"]) {
    assert.equal(isStale({ status, created_at: at(48 * 60) }, NOW), false, status);
  }
});

test("no task at all is not stale", () => {
  assert.equal(isStale(null, NOW), false);
  assert.equal(isStale(undefined, NOW), false);
});

test("a missing or unparseable timestamp keeps waiting rather than accusing", () => {
  // No evidence of being stuck is not evidence of being stuck.
  for (const created of [undefined, "", "not-a-date", null]) {
    assert.equal(isStale({ status: "queued", created_at: created }, NOW), false);
  }
});

// ── the component actually applies it ────────────────────────────────

test("the component defines the same threshold", () => {
  assert.match(SOURCE, /const STALE_AFTER_MS = 15 \* 60 \* 1000;/);
});

test("the analysing message is gated on not being stale", () => {
  // The bug was this block rendering unconditionally for queued tasks.
  assert.match(SOURCE, /\{running && !stale && \(/);
  assert.match(SOURCE, /\{running && stale && \(/);
});

test("the stale message does not claim work is happening", () => {
  const block = SOURCE.slice(SOURCE.indexOf("{running && stale && ("));
  const shown = block.slice(0, 900);
  assert.ok(!/Analyzing/i.test(shown), "the stale branch still says Analyzing");
  assert.match(shown, /taking longer than it should/i);
  // It must be actionable and must not imply data loss.
  assert.match(shown, /Start over/);
  assert.match(shown, /nothing has been lost|still in the queue/i);
});

test("polling is not abandoned when a job goes stale", () => {
  // `running` stays true, so the existing poll effect keeps running and a
  // late worker pickup still resolves the screen.
  assert.match(
    SOURCE,
    /const running = task\?\.status === "queued" \|\| task\?\.status === "running";/,
  );
});

test("the timestamp the check needs is carried on the task", () => {
  assert.match(SOURCE, /created_at\?: string;/);
  const page = fs.readFileSync(
    path.join(__dirname, "..", "app", "dashboard", "clipping", "page.tsx"),
    "utf8",
  );
  assert.match(page, /created_at: latest\.created_at/);
});

test("the ticking clock only runs while something is outstanding", () => {
  assert.match(SOURCE, /if \(!running\) return;\s*\n\s*const timer = setInterval/);
  assert.match(SOURCE, /clearInterval\(timer\)/);
});
