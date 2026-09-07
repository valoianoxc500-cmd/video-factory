/**
 * The V4 signature is cryptographically valid, not merely well shaped.
 *
 * media.test.cjs checks that the URL carries the right parameters. That would
 * still pass if the string-to-sign were built wrongly -- GCS would then reject
 * every URL with SignatureDoesNotMatch, and we would only find out once a real
 * key existed.
 *
 * So this rebuilds the canonical request from the returned URL alone, exactly
 * as Google's documented V4 algorithm says a verifier must, and checks the
 * signature against the public key. If the implementation used the wrong hash,
 * scope, header set or parameter ordering, verification fails here.
 *
 * A throwaway keypair is generated per run; no real credential is involved.
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const { generateKeyPairSync, createVerify, createHash } = require("node:crypto");

const media = require("../.test-build/media.js");

const { privateKey, publicKey } = generateKeyPairSync("rsa", {
  modulusLength: 2048,
});
const CLIENT_EMAIL = "signer@project-test.iam.gserviceaccount.com";
const BUCKET = "video-factory-media-test";
const OBJECT = "videos/horror_stories/vf_20260905121606_46bc7f90/video.mp4";

function withSigning(fn) {
  process.env.GCS_SERVICE_ACCOUNT_JSON = JSON.stringify({
    client_email: CLIENT_EMAIL,
    private_key: privateKey.export({ type: "pkcs8", format: "pem" }),
  });
  process.env.GCS_BUCKET = BUCKET;
  try {
    return fn();
  } finally {
    delete process.env.GCS_SERVICE_ACCOUNT_JSON;
    delete process.env.GCS_BUCKET;
  }
}

/**
 * Rebuild the string-to-sign from a signed URL, per Google's V4 spec.
 * Deliberately written from the specification rather than by reusing the
 * signing code, so a mistake in that code cannot cancel itself out here.
 */
function stringToSignFrom(urlString) {
  const url = new URL(urlString);

  const signedQuery = [...url.searchParams.entries()]
    .filter(([key]) => key !== "X-Goog-Signature")
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`)
    .join("&");

  const canonicalRequest = [
    "GET",
    url.pathname,
    signedQuery,
    `host:${url.host}`,
    "",
    "host",
    "UNSIGNED-PAYLOAD",
  ].join("\n");

  const stamp = url.searchParams.get("X-Goog-Date");
  const credential = url.searchParams.get("X-Goog-Credential") ?? "";
  const scope = credential.split("/").slice(1).join("/");

  return [
    "GOOG4-RSA-SHA256",
    stamp,
    scope,
    createHash("sha256").update(canonicalRequest).digest("hex"),
  ].join("\n");
}

test("the signature verifies against the public key", () => {
  withSigning(() => {
    const url = media.signedUrl(OBJECT, 900);
    const signature = new URL(url).searchParams.get("X-Goog-Signature");

    const ok = createVerify("RSA-SHA256")
      .update(stringToSignFrom(url))
      .verify(publicKey, Buffer.from(signature, "hex"));

    assert.ok(
      ok,
      "the signature does not match a canonical request rebuilt per Google's " +
        "V4 spec -- GCS would reject this URL with SignatureDoesNotMatch",
    );
  });
});

test("a tampered path invalidates the signature", () => {
  withSigning(() => {
    const url = media.signedUrl(OBJECT, 900);
    const tampered = url.replace("video.mp4", "other.mp4");
    const signature = new URL(tampered).searchParams.get("X-Goog-Signature");

    const ok = createVerify("RSA-SHA256")
      .update(stringToSignFrom(tampered))
      .verify(publicKey, Buffer.from(signature, "hex"));

    assert.equal(ok, false, "a URL edited after signing still verified");
  });
});

test("a tampered expiry invalidates the signature", () => {
  withSigning(() => {
    const url = new URL(media.signedUrl(OBJECT, 900));
    const signature = url.searchParams.get("X-Goog-Signature");
    url.searchParams.set("X-Goog-Expires", "604800");

    const ok = createVerify("RSA-SHA256")
      .update(stringToSignFrom(url.toString()))
      .verify(publicKey, Buffer.from(signature, "hex"));

    assert.equal(ok, false, "the expiry can be extended without resigning");
  });
});

test("the credential scope is dated today and region-neutral", () => {
  withSigning(() => {
    const url = new URL(media.signedUrl(OBJECT));
    const credential = url.searchParams.get("X-Goog-Credential") ?? "";
    const [email, date, region, service, request] = credential.split("/");

    assert.equal(email, CLIENT_EMAIL);
    assert.equal(region, "auto");
    assert.equal(service, "storage");
    assert.equal(request, "goog4_request");

    const today = new Date().toISOString().slice(0, 10).replace(/-/g, "");
    assert.equal(date, today, "the credential scope date must match X-Goog-Date");
    assert.equal(date, url.searchParams.get("X-Goog-Date")?.slice(0, 8));
  });
});

test("the timestamp is basic-format ISO 8601, as V4 requires", () => {
  withSigning(() => {
    const stamp = new URL(media.signedUrl(OBJECT)).searchParams.get("X-Goog-Date");
    // e.g. 20260905T215159Z -- no dashes, colons or fractional seconds.
    assert.match(stamp ?? "", /^\d{8}T\d{6}Z$/);
  });
});

test("two URLs for the same object differ", () => {
  withSigning(() => {
    // Same second, same inputs: the point is that nothing is cached across
    // calls in a way that would outlive the expiry window.
    const a = media.signedUrl(OBJECT, 900);
    const b = media.signedUrl(OBJECT, 1800);
    assert.notEqual(a, b);
  });
});
