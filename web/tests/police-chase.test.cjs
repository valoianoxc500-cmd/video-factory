const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const chase = require("../.test-build/police-chase.js");
const root = path.join(__dirname, "..");
const read = (file) => fs.readFileSync(path.join(root, file), "utf8");

test("Police Chase options match the product contract", () => {
  assert.deepEqual(chase.CHASE_LENGTHS, [15, 30, 45, 60, 90]);
  assert.deepEqual(chase.CHASE_COUNTS, [1, 2, 3, 4, 5]);
  assert.deepEqual(chase.CAPTION_LANGUAGES.map((x) => x.id), ["auto", "en", "ar", "none"]);
  assert.deepEqual(chase.CTA_MODES.map((x) => x.id), ["auto", "custom", "off"]);
  assert.equal(chase.defaultChaseOptions().ctaPlacement, "end");
});

test("customer errors do not expose render or filesystem internals", () => {
  for (const raw of ["ffmpeg filter failed", "Traceback in provider", "C:\\tmp\\clip.mp4", "/tmp/job/source.mp4"]) {
    const safe = chase.safeChaseError(raw);
    assert.doesNotMatch(safe, /ffmpeg|traceback|provider|[a-z]:\\|\/tmp\//i);
  }
});

test("studio uses existing authorized upload and sends every visible option", () => {
  const ui = read("components/reels/PoliceChaseStudio.tsx");
  assert.match(ui, /\/api\/reels\/clips\/upload/);
  assert.match(ui, /\/api\/police-chase/);
  for (const field of ["targetSeconds", "count", "captionLanguage", "ctaMode", "ctaText", "ctaPlacement", "sourceMetadata", "ownsOrPermitted"]) {
    assert.match(ui, new RegExp(field));
  }
  assert.match(ui, /I own this footage or have permission to reuse it/);
});

test("route preserves safe URL ingest and processable-source checks", () => {
  const route = read("app/api/police-chase/route.ts");
  assert.match(route, /mediaImportPlan\(parsed,connected\)/);
  assert.match(route, /if\(!plan\.canFetchMedia\)/);
  assert.match(route, /storage_path/);
  assert.match(route, /product:"police_chase"/);
  assert.doesNotMatch(route, /yt-dlp|youtube-dl|scrap|bypass/i);
});

test("saved projects are isolated by product in the existing user-owned task store", () => {
  const repo = read("lib/police-chase-repository.ts");
  assert.match(repo, /\.eq\("kind","process"\)/);
  assert.match(repo, /\.contains\("payload",\{product:"police_chase"\}\)/);
});

test("sidebar exposes one independent Police Chase Studio destination", () => {
  const sidebar = read("components/Sidebar.tsx");
  assert.match(sidebar, /href="\/dashboard\/police-chase"/);
  assert.match(sidebar, />Police Chase Studio</);
});

test("implementation contains no face recognition or identity tracking", () => {
  const sources = [
    read("components/reels/PoliceChaseStudio.tsx"),
    read("app/api/police-chase/route.ts"),
    read("../viral/police_chase.py"),
  ].join("\n");
  assert.doesNotMatch(sources, /face recognition|facial recognition|identify person/i);
});
