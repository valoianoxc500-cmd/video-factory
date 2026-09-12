/**
 * An AI Video job must never display as progress forever.
 *
 * In production a job sat `queued` with `attempts = 0` and `claimed_at = null`
 * while the screen showed "Waiting to start — 0%". No worker had ever claimed
 * it, so nothing would ever change the status and the state had no end
 * condition. Nothing in the pipeline could report it either, because the
 * pipeline never ran.
 *
 * The safeguard lives in the read path and is a *view*, not a write: a stalled
 * job stays queued, keeps its place, and is still claimed and finished when a
 * worker returns. It must never produce a duplicate.
 *
 * Run with: npm run test  (compiles lib/ first -- see package.json)
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { progressLabel } = require("../.test-build/aivideo.js");

// Line endings normalised: the working copy is CRLF, and a `\n}\n` search for
// the end of a function silently misses, which made the "never writes" check
// scan the POST handler below it and fail on an insert that is not in scope.
const ROUTE = fs
  .readFileSync(
    path.join(__dirname, "..", "app", "api", "aivideo", "jobs", "route.ts"),
    "utf8",
  )
  .replace(/\r\n/g, "\n");

// ── what the customer is told ───────────────────────────────────────

test("a job queued moments ago is not called stalled", () => {
  assert.equal(
    progressLabel({ status: "queued", stage: "", message: "" }),
    "Waiting to start",
  );
});

test("a stalled job reads as delayed, not failed", () => {
  const label = progressLabel({
    status: "queued", stage: "", message: "", stalled: true,
  });
  assert.match(label, /delayed/i);
  assert.doesNotMatch(label, /fail|error/i);
});

test("a finished job is never reported as stalled", () => {
  assert.equal(
    progressLabel({ status: "done", stage: "", message: "", stalled: true }),
    "Ready",
  );
});

test("an errored job keeps its own wording", () => {
  assert.equal(
    progressLabel({ status: "error", stage: "", message: "", stalled: true }),
    "Needs another try",
  );
});

test("a running job shows the stage it is on", () => {
  assert.equal(
    progressLabel({ status: "running", stage: "footage", message: "" }),
    "Finding footage",
  );
});

test("no label names a provider, a stage id or the worker", () => {
  const labels = [
    progressLabel({ status: "queued", stage: "", message: "", stalled: true }),
    progressLabel({ status: "running", stage: "render", message: "" }),
    progressLabel({ status: "error", stage: "", message: "" }),
    progressLabel({ status: "queued", stage: "", message: "" }),
  ];
  for (const label of labels) {
    for (const leak of ["pexels", "edge", "ffmpeg", "http", "worker", "null"]) {
      assert.ok(
        !label.toLowerCase().includes(leak),
        `"${leak}" leaked into "${label}"`,
      );
    }
  }
});

// ── the rule itself ─────────────────────────────────────────────────

test("the stall thresholds are bounded and generous", () => {
  const unclaimed = /UNCLAIMED_STALL_MS = (\d+) \* 60 \* 1000/.exec(ROUTE);
  const silent = /SILENT_STALL_MS = (\d+) \* 60 \* 1000/.exec(ROUTE);
  assert.ok(unclaimed, "UNCLAIMED_STALL_MS not found in the route");
  assert.ok(silent, "SILENT_STALL_MS not found in the route");

  // Long enough that a normal render is never flagged (the slowest stage
  // observed is the render at ~4 minutes); short enough that a customer is
  // not left guessing.
  assert.ok(Number(unclaimed[1]) >= 5 && Number(unclaimed[1]) <= 30);
  assert.ok(Number(silent[1]) >= Number(unclaimed[1]));
});

test("the safeguard annotates and never writes", () => {
  const start = ROUTE.indexOf("function withStallState");
  assert.ok(start > 0, "withStallState not found");
  const body = ROUTE.slice(start, ROUTE.indexOf("\n}\n", start));

  // No writes: a stalled job must stay exactly where it is, still claimable,
  // and a duplicate must be impossible.
  for (const forbidden of [".insert(", ".update(", ".upsert(", ".delete(", ".rpc("]) {
    assert.ok(!body.includes(forbidden), `withStallState performs a ${forbidden}`);
  }
});

test("only queued and running jobs can be flagged", () => {
  const start = ROUTE.indexOf("function withStallState");
  const body = ROUTE.slice(start, ROUTE.indexOf("\n}\n", start));
  assert.ok(body.includes('!== "queued" && job.status !== "running"'));
});

test("the read path selects the timestamps the rule needs", () => {
  assert.ok(ROUTE.includes("updated_at"), "updated_at is not selected");
  assert.ok(ROUTE.includes("claimed_at"), "claimed_at is not selected");
});
