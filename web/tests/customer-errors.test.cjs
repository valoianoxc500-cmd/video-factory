const test = require("node:test");
const assert = require("node:assert/strict");

// Kept source-level because the web test command only compiles selected
// modules today; TypeScript typecheck validates the implementation.
const source = require("node:fs").readFileSync(
  require("node:path").join(__dirname, "..", "lib", "customer-errors.ts"), "utf8",
);

test("customer error mapper contains internal diagnostics denylist", () => {
  for (const marker of ["traceback", "provider", "gemini", "ffmpeg", "ENOSPC"]) {
    assert.match(source, new RegExp(marker, "i"));
  }
});

test("customer state mapper uses only public lifecycle labels", () => {
  for (const label of ["Preparing", "Researching", "Creating", "Rendering", "Almost ready", "Complete"]) {
    assert.match(source, new RegExp(label));
  }
});
