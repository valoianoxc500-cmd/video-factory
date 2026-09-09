/**
 * Clipping's customer boundary, on the web side.
 *
 * The worker sanitises what it raises, but a task row written before that
 * existed can still carry a filter graph in its error column, and the screen
 * renders whatever the row says. So the mapping is tested here too: five
 * states and nothing else, and no internal detail on screen.
 *
 * Run with: npm run test  (compiles lib/ first -- see package.json)
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const clipping = require("../.test-build/clipping.js");

// ── options ──────────────────────────────────────────────────────────

test("the option ids match the worker's vocabulary", () => {
  assert.deepEqual(clipping.ASPECTS.map((a) => a.id), ["9:16", "1:1", "4:5", "16:9"]);
  assert.deepEqual(clipping.QUALITIES.map((q) => q.id), ["high", "balanced", "fast"]);
  assert.deepEqual(clipping.FOCUS_MODES.map((f) => f.id), ["auto", "center", "left", "right"]);
  assert.deepEqual(clipping.CAPTION_STYLES.map((c) => c.id), ["clean", "bold", "minimal"]);
});

test("the defaults are vertical, balanced, subject-following, captions off", () => {
  const defaults = clipping.defaultClipOptions();
  assert.equal(defaults.aspect, "9:16");
  assert.equal(defaults.quality, "balanced");
  assert.equal(defaults.focus, "auto");
  assert.equal(defaults.captions, false);
});

test("every default is a real option id", () => {
  const defaults = clipping.defaultClipOptions();
  assert.ok(clipping.ASPECTS.some((a) => a.id === defaults.aspect));
  assert.ok(clipping.QUALITIES.some((q) => q.id === defaults.quality));
  assert.ok(clipping.FOCUS_MODES.some((f) => f.id === defaults.focus));
  assert.ok(clipping.CAPTION_STYLES.some((c) => c.id === defaults.caption_style));
});

// ── the five states ──────────────────────────────────────────────────

test("task statuses map to customer states", () => {
  assert.equal(clipping.customerState("queued"), "Preparing");
  assert.equal(clipping.customerState("running"), "Creating clips");
  assert.equal(clipping.customerState("done"), "Ready");
});

test("a failed or unknown status yields no state to display", () => {
  // Failure is rendered as a message, not as a progress state.
  assert.equal(clipping.customerState("failed"), null);
  assert.equal(clipping.customerState("ffmpeg_pass_2"), null);
  assert.equal(clipping.customerState(""), null);
  assert.equal(clipping.customerState(null), null);
});

test("any state shown is one of the five", () => {
  for (const status of ["queued", "running", "done", "QUEUED", " Running "]) {
    const state = clipping.customerState(status);
    assert.ok(clipping.CUSTOMER_STATES.includes(state), `${status} -> ${state}`);
  }
});

// ── safe errors ──────────────────────────────────────────────────────

const LEAKY = [
  "ffmpeg exited with code 1",
  "Error opening filters: Invalid argument",
  "/tmp/vrf_process_ab12/source.mp4: No such file",
  "C:\\Users\\kokoi\\out.mp4 could not be written",
  "libx264 encoder error",
  "https://storage.googleapis.com/bucket/x.mp4 403",
  "Traceback (most recent call last)",
];

test("internal detail never reaches the screen", () => {
  for (const raw of LEAKY) {
    const message = clipping.safeClipError(raw);
    assert.ok(
      !/ffmpeg|libx264|traceback|https?:\/\/|[a-z]:\\/i.test(message),
      `leaked: ${message}`,
    );
    assert.ok(message.length > 0);
  }
});

test("an empty or missing error still reads as a sentence", () => {
  for (const raw of ["", null, undefined]) {
    assert.match(clipping.safeClipError(raw), /could not be created/i);
  }
});

test("a long message is replaced rather than truncated mid-word", () => {
  const message = clipping.safeClipError("x".repeat(400));
  assert.ok(message.length < 200);
  assert.ok(!message.includes("xxxx"));
});

test("a short customer-written reason is preserved", () => {
  const reason = "Those trims leave nothing behind.";
  assert.equal(clipping.safeClipError(reason), reason);
});

test("a partial-failure message survives the filter", () => {
  const reason = "That clip could not be created. Your other clips are unaffected.";
  assert.equal(clipping.safeClipError(reason), reason);
});
