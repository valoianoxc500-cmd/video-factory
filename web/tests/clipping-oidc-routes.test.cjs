const test = require("node:test");
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");

const root = join(__dirname, "..");
const uploadRoute = readFileSync(
  join(root, "app/api/reels/clips/upload/route.ts"),
  "utf8",
);
const mediaRoute = readFileSync(
  join(root, "app/api/reels/clips/media/route.ts"),
  "utf8",
);

test("upload route forwards the incoming Vercel OIDC token to signing", () => {
  assert.match(uploadRoute, /request\.headers\.get\("x-vercel-oidc-token"\)/);
  assert.match(
    uploadRoute,
    /signedUploadUrl\([\s\S]*?objectPath,[\s\S]*?contentType \|\| "video\/mp4",[\s\S]*?undefined,[\s\S]*?oidcToken,[\s\S]*?\)/,
  );
});

test("media route forwards the incoming Vercel OIDC token to read signing", () => {
  assert.match(mediaRoute, /request\.headers\.get\("x-vercel-oidc-token"\)/);
  assert.match(mediaRoute, /signedReadUrl\(stored, undefined, oidcToken\)/);
});

test("routes keep customer-safe errors when federation has no token", () => {
  assert.match(uploadRoute, /Uploads are not available right now\./);
  assert.match(mediaRoute, /That video is not available right now\./);
  assert.doesNotMatch(uploadRoute, /error:\s*(?:err|error)\.message/);
  assert.doesNotMatch(mediaRoute, /error:\s*(?:err|error)\.message/);
});
