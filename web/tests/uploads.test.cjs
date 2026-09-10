/**
 * Direct uploads for Clipping: path safety and signing shape.
 *
 * These exist because this is the code that decides *which object* a browser
 * is allowed to write to. A path validator that is one character too generous
 * lets one customer write into another's prefix, so the validator is tested
 * against traversal and cross-tenant shapes rather than only the happy path.
 *
 * A throwaway RSA key is generated per run: nothing real is needed to prove
 * the signature shape, and no credential belongs in a test file.
 *
 * Run with: npm run test  (compiles lib/ first -- see package.json)
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const { generateKeyPairSync } = require("node:crypto");

const uploads = require("../.test-build/uploads.js");

const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const TEST_SA = JSON.stringify({
  client_email: "uploader@example.iam.gserviceaccount.com",
  private_key: privateKey.export({ type: "pkcs8", format: "pem" }),
});

const USER = "11111111-2222-4333-8444-555555555555";
const UPLOAD = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const GOOD = `vrf/uploads/${USER}/${UPLOAD}/clip.mp4`;

function withStorage(fn) {
  const sa = process.env.GCS_SERVICE_ACCOUNT_JSON;
  const bucket = process.env.GCS_BUCKET;
  process.env.GCS_SERVICE_ACCOUNT_JSON = TEST_SA;
  process.env.GCS_BUCKET = "test-bucket";
  try {
    return fn();
  } finally {
    if (sa === undefined) delete process.env.GCS_SERVICE_ACCOUNT_JSON;
    else process.env.GCS_SERVICE_ACCOUNT_JSON = sa;
    if (bucket === undefined) delete process.env.GCS_BUCKET;
    else process.env.GCS_BUCKET = bucket;
  }
}

// ── accepted files ───────────────────────────────────────────────────

test("the formats the screen advertises are the formats accepted", () => {
  for (const type of ["video/mp4", "video/quicktime", "video/webm", "video/x-matroska"]) {
    assert.ok(uploads.isAcceptedType(type), type);
  }
});

test("the advertised limit matches the enforced limit", () => {
  // The screen states "up to 2GB" as a literal; this is what actually stops.
  assert.equal(uploads.MAX_UPLOAD_BYTES, 2 * 1024 * 1024 * 1024);
  assert.match(uploads.describeLimits(), /2GB/);
  assert.match(uploads.describeLimits(), /MP4, MOV, WebM or MKV/);
});

test("a generic content type falls back to the extension", () => {
  // Browsers send an empty or octet-stream type for .mkv and .mov.
  assert.ok(uploads.isAcceptedType("", "holiday.mkv"));
  assert.ok(uploads.isAcceptedType("application/octet-stream", "a.mov"));
});

test("non-video files are refused", () => {
  assert.equal(uploads.isAcceptedType("image/png", "a.png"), false);
  assert.equal(uploads.isAcceptedType("application/pdf", "a.pdf"), false);
  assert.equal(uploads.isAcceptedType("", "notes.txt"), false);
  assert.equal(uploads.isAcceptedType("text/html", "a.mp4.html"), false);
});

// ── filenames ────────────────────────────────────────────────────────

test("a filename is reduced to something safe to put in a path", () => {
  assert.equal(uploads.safeFilename("my holiday!.mp4"), "my_holiday_.mp4");
  assert.equal(uploads.safeFilename("../../etc/passwd"), "passwd");
  assert.equal(uploads.safeFilename("C:\\Users\\me\\a.mp4"), "a.mp4");
  assert.equal(uploads.safeFilename(""), "upload.mp4");
  assert.equal(uploads.safeFilename("..."), "upload.mp4");
});

test("a very long filename is bounded", () => {
  const name = uploads.safeFilename("x".repeat(400) + ".mp4");
  assert.ok(name.length <= 80);
});

// ── path safety ──────────────────────────────────────────────────────

test("a well-formed upload path is accepted", () => {
  assert.equal(uploads.assertUploadPath(GOOD), GOOD);
});

const HOSTILE = [
  "",
  "/vrf/uploads/a/b/c.mp4",
  `vrf/uploads/${USER}/${UPLOAD}/../../../secret.mp4`,
  `vrf/uploads/${USER}//${UPLOAD}/c.mp4`,
  `vrf/uploads/${USER}/${UPLOAD}/a\\b.mp4`,
  `videos/channel/x/y.mp4`,
  `vrf/uploads/${USER}/${UPLOAD}/sub/dir.mp4`,
  `vrf/uploads/not-a-uuid/${UPLOAD}/c.mp4`,
  `vrf/uploads/${USER}/${UPLOAD}/`,
  `vrf/processed/${USER}/${UPLOAD}/c.mp4`,
];

test("hostile object paths are refused", () => {
  for (const path of HOSTILE) {
    assert.throws(
      () => uploads.assertUploadPath(path),
      /unexpected object path/i,
      `accepted: ${path}`,
    );
  }
});

test("the upload path is built from the session's user id", () => {
  const path = uploads.uploadObjectPath(USER, UPLOAD, "my video.mp4");
  assert.equal(path, `vrf/uploads/${USER}/${UPLOAD}/my_video.mp4`);
  assert.ok(path.startsWith(`vrf/uploads/${USER}/`));
});

test("a read path from the library cannot be signed for upload", () => {
  // `media.ts` guards `videos/...` for GET; this guards `vrf/uploads/...`
  // for PUT. Neither may sign the other's objects.
  assert.throws(() => uploads.assertUploadPath("videos/horror/abc/def.mp4"));
});

// ── signing ──────────────────────────────────────────────────────────

test("a signed upload URL is a PUT for exactly that object", () => {
  withStorage(() => {
    const signed = uploads.signedUploadUrl(GOOD, "video/mp4");
    assert.match(signed.url, /^https:\/\/storage\.googleapis\.com\/test-bucket\//);
    assert.ok(signed.url.includes(encodeURIComponent(UPLOAD)) || signed.url.includes(UPLOAD));
    assert.match(signed.url, /X-Goog-Algorithm=GOOG4-RSA-SHA256/);
    assert.match(signed.url, /X-Goog-Signature=[0-9a-f]+$/);
  });
});

test("content type is a signed header, so the URL cannot be reused for anything else", () => {
  withStorage(() => {
    const signed = uploads.signedUploadUrl(GOOD, "video/mp4");
    assert.match(signed.url, /X-Goog-SignedHeaders=content-type%3Bhost/);
    assert.equal(signed.headers["Content-Type"], "video/mp4");
  });
});

test("a different content type produces a different signature", () => {
  withStorage(() => {
    const a = uploads.signedUploadUrl(GOOD, "video/mp4");
    const b = uploads.signedUploadUrl(GOOD, "video/webm");
    assert.notEqual(
      a.url.split("X-Goog-Signature=")[1],
      b.url.split("X-Goog-Signature=")[1],
    );
  });
});

test("the URL expires", () => {
  withStorage(() => {
    const signed = uploads.signedUploadUrl(GOOD, "video/mp4", 900);
    assert.equal(signed.expiresInSeconds, 900);
    assert.match(signed.url, /X-Goog-Expires=900/);
  });
});

test("an absurd ttl is clamped rather than honoured", () => {
  withStorage(() => {
    assert.equal(uploads.signedUploadUrl(GOOD, "video/mp4", 1).expiresInSeconds, 60);
    assert.equal(
      uploads.signedUploadUrl(GOOD, "video/mp4", 99999999).expiresInSeconds,
      60 * 60 * 12,
    );
  });
});

test("signing a hostile path throws instead of returning a URL", () => {
  withStorage(() => {
    for (const path of HOSTILE) {
      assert.throws(() => uploads.signedUploadUrl(path, "video/mp4"));
    }
  });
});

// ── configuration ────────────────────────────────────────────────────

test("missing storage configuration is reported, not guessed around", () => {
  const sa = process.env.GCS_SERVICE_ACCOUNT_JSON;
  const bucket = process.env.GCS_BUCKET;
  delete process.env.GCS_SERVICE_ACCOUNT_JSON;
  delete process.env.GCS_BUCKET;
  try {
    assert.equal(uploads.uploadsAvailable(), false);
    assert.throws(() => uploads.signedUploadUrl(GOOD, "video/mp4"), /not configured/i);
  } finally {
    if (sa !== undefined) process.env.GCS_SERVICE_ACCOUNT_JSON = sa;
    if (bucket !== undefined) process.env.GCS_BUCKET = bucket;
  }
});

test("uploadsAvailable is true once configured", () => {
  withStorage(() => assert.equal(uploads.uploadsAvailable(), true));
});

// ── the URL the worker will download ─────────────────────────────────

test("the public URL matches the shape the worker stores and re-reads", () => {
  withStorage(() => {
    // Mirrors worker/storage.py::_gcs_public_url. If these drift, an uploaded
    // video is unreachable to the worker that has to process it.
    assert.equal(
      uploads.publicUploadUrl(GOOD),
      `https://storage.googleapis.com/test-bucket/${GOOD}`,
    );
  });
});

test("a hostile path cannot produce a public URL either", () => {
  withStorage(() => {
    for (const path of HOSTILE) {
      assert.throws(() => uploads.publicUploadUrl(path));
    }
  });
});
