/**
 * Direct uploads for Clipping: path safety, and signing without a key.
 *
 * Two things are being protected here.
 *
 * The path validator decides *which object* a browser may write to, so it is
 * tested against traversal and cross-tenant shapes rather than only the happy
 * path — one character too generous and a customer writes into another's
 * prefix.
 *
 * The signer no longer has a private key in production: the organisation
 * enforces `iam.disableServiceAccountKeyCreation`, so Google performs the RSA
 * signature remotely. These tests cover both credential paths and, crucially,
 * that federation wins when both are present — a stray development key must
 * never quietly become the production signer.
 *
 * Offline throughout: `fetch` is stubbed, and the local-key tests use a
 * throwaway RSA key generated per run. No credential belongs in a test file.
 *
 * Run with: npm run test  (compiles lib/ first -- see package.json)
 */

const test = require("node:test");
const assert = require("node:assert/strict");
const { generateKeyPairSync } = require("node:crypto");

const uploads = require("../.test-build/uploads.js");
const auth = require("../.test-build/gcs-auth.js");

const { privateKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const TEST_SA = JSON.stringify({
  client_email: "uploader@example.iam.gserviceaccount.com",
  private_key: privateKey.export({ type: "pkcs8", format: "pem" }),
});

const USER = "11111111-2222-4333-8444-555555555555";
const UPLOAD = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const GOOD = `vrf/uploads/${USER}/${UPLOAD}/clip.mp4`;

const PROVIDER =
  "projects/123456789/locations/global/workloadIdentityPools/vercel/providers/vercel-oidc";
const SA_EMAIL = "video-factory@project-c7d39787-bbc0-4b81-8ba.iam.gserviceaccount.com";

const ENV_KEYS = [
  "GCS_SERVICE_ACCOUNT_JSON",
  "GCS_BUCKET",
  "GCP_WORKLOAD_IDENTITY_PROVIDER",
  "GCP_SERVICE_ACCOUNT_EMAIL",
  "VERCEL_OIDC_TOKEN",
];

/** Run `fn` with an exact environment and a stubbed fetch, then restore. */
async function withEnv(env, fn, fetchImpl) {
  const saved = {};
  for (const key of ENV_KEYS) {
    saved[key] = process.env[key];
    delete process.env[key];
  }
  const realFetch = global.fetch;
  const realError = console.error;
  const logs = [];
  console.error = (...args) => logs.push(args.join(" "));
  for (const [key, value] of Object.entries(env)) process.env[key] = value;
  if (fetchImpl) global.fetch = fetchImpl;
  auth.resetTokenCache();

  try {
    return await fn(logs);
  } finally {
    global.fetch = realFetch;
    console.error = realError;
    for (const key of ENV_KEYS) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
    auth.resetTokenCache();
  }
}

const KEY_ENV = { GCS_SERVICE_ACCOUNT_JSON: TEST_SA, GCS_BUCKET: "test-bucket" };
const FED_ENV = {
  GCS_BUCKET: "test-bucket",
  GCP_WORKLOAD_IDENTITY_PROVIDER: PROVIDER,
  GCP_SERVICE_ACCOUNT_EMAIL: SA_EMAIL,
  VERCEL_OIDC_TOKEN: "header.payload.signature",
};

/** A fetch that plays the STS -> impersonate -> signBlob sequence. */
function googleStub(overrides = {}) {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url: String(url), body: JSON.parse(init.body), headers: init.headers });
    const target = String(url);
    if (target.includes("sts.googleapis.com")) {
      return jsonResponse(overrides.sts ?? { access_token: "federated-token" });
    }
    if (target.includes(":generateAccessToken")) {
      return jsonResponse(
        overrides.impersonate ?? {
          accessToken: "sa-access-token",
          expireTime: new Date(Date.now() + 3600_000).toISOString(),
        },
      );
    }
    if (target.includes(":signBlob")) {
      return jsonResponse(
        overrides.sign ?? { signedBlob: Buffer.from("signature-bytes").toString("base64") },
      );
    }
    throw new Error(`unexpected call: ${target}`);
  };
  impl.calls = calls;
  return impl;
}

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  };
}

// ── accepted files ───────────────────────────────────────────────────

test("the formats the screen advertises are the formats accepted", () => {
  for (const type of ["video/mp4", "video/quicktime", "video/webm", "video/x-matroska"]) {
    assert.ok(uploads.isAcceptedType(type), type);
  }
});

test("the advertised limit matches the enforced limit", () => {
  assert.equal(uploads.MAX_UPLOAD_BYTES, 2 * 1024 * 1024 * 1024);
  assert.match(uploads.describeLimits(), /2GB/);
  assert.match(uploads.describeLimits(), /MP4, MOV, WebM or MKV/);
});

test("a generic content type falls back to the extension", () => {
  assert.ok(uploads.isAcceptedType("", "holiday.mkv"));
  assert.ok(uploads.isAcceptedType("application/octet-stream", "a.mov"));
});

test("non-video files are refused", () => {
  assert.equal(uploads.isAcceptedType("image/png", "a.png"), false);
  assert.equal(uploads.isAcceptedType("application/pdf", "a.pdf"), false);
  assert.equal(uploads.isAcceptedType("", "notes.txt"), false);
  assert.equal(uploads.isAcceptedType("text/html", "a.mp4.html"), false);
});

// ── filenames and path safety ────────────────────────────────────────

test("a filename is reduced to something safe to put in a path", () => {
  assert.equal(uploads.safeFilename("my holiday!.mp4"), "my_holiday_.mp4");
  assert.equal(uploads.safeFilename("../../etc/passwd"), "passwd");
  assert.equal(uploads.safeFilename("C:\\Users\\me\\a.mp4"), "a.mp4");
  assert.equal(uploads.safeFilename(""), "upload.mp4");
  assert.equal(uploads.safeFilename("..."), "upload.mp4");
});

test("a very long filename is bounded", () => {
  assert.ok(uploads.safeFilename("x".repeat(400) + ".mp4").length <= 80);
});

test("a well-formed upload path is accepted", () => {
  assert.equal(uploads.assertUploadPath(GOOD), GOOD);
});

const HOSTILE = [
  "",
  "/vrf/uploads/a/b/c.mp4",
  `vrf/uploads/${USER}/${UPLOAD}/../../../secret.mp4`,
  `vrf/uploads/${USER}//${UPLOAD}/c.mp4`,
  `vrf/uploads/${USER}/${UPLOAD}/a\\b.mp4`,
  "videos/channel/x/y.mp4",
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
});

test("a read path from the library cannot be signed for upload", () => {
  assert.throws(() => uploads.assertUploadPath("videos/horror/abc/def.mp4"));
});

// ── which credential signs ───────────────────────────────────────────

test("federation is preferred when both credentials are present", async () => {
  // A key left in the environment must not become the production signer.
  await withEnv({ ...KEY_ENV, ...FED_ENV }, async () => {
    const signer = uploads.signerIdentity();
    assert.equal(signer.mode, "federated");
    assert.equal(signer.email, SA_EMAIL);
  });
});

test("a local key is used when federation is not configured", async () => {
  await withEnv(KEY_ENV, async () => {
    const signer = uploads.signerIdentity();
    assert.equal(signer.mode, "key");
    assert.equal(signer.email, "uploader@example.iam.gserviceaccount.com");
  });
});

test("no credential at all is reported with both options named", async () => {
  await withEnv({ GCS_BUCKET: "test-bucket" }, async () => {
    assert.equal(uploads.uploadsAvailable(), false);
    assert.throws(
      () => uploads.signerIdentity(),
      /GCP_WORKLOAD_IDENTITY_PROVIDER[\s\S]*GCS_SERVICE_ACCOUNT_JSON/,
    );
  });
});

test("uploadsAvailable is true with federation alone -- no key needed", async () => {
  await withEnv(FED_ENV, async () => {
    assert.equal(uploads.uploadsAvailable(), true);
  });
});

// ── federated signing ────────────────────────────────────────────────

test("a keyless signed URL is a PUT for exactly that object", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    const signed = await uploads.signedUploadUrl(GOOD, "video/mp4");
    assert.match(signed.url, /^https:\/\/storage\.googleapis\.com\/test-bucket\//);
    assert.match(signed.url, /X-Goog-Algorithm=GOOG4-RSA-SHA256/);
    assert.match(signed.url, /X-Goog-Signature=[0-9a-f]+$/);
  }, stub);
});

test("the signature is hex, not the base64 Google returns", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    const signed = await uploads.signedUploadUrl(GOOD, "video/mp4");
    const signature = signed.url.split("X-Goog-Signature=")[1];
    // base64 in a V4 query string produces a URL that looks right and is
    // always rejected.
    assert.match(signature, /^[0-9a-f]+$/);
    assert.equal(
      Buffer.from(signature, "hex").toString(),
      "signature-bytes",
    );
  }, stub);
});

test("the URL credits the impersonated service account", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    const signed = await uploads.signedUploadUrl(GOOD, "video/mp4");
    assert.ok(signed.url.includes(encodeURIComponent(SA_EMAIL)));
  }, stub);
});

test("the exchange follows STS then impersonation then signing", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    await uploads.signedUploadUrl(GOOD, "video/mp4");
  }, stub);

  const hosts = stub.calls.map((c) => c.url);
  assert.match(hosts[0], /sts\.googleapis\.com/);
  assert.match(hosts[1], /:generateAccessToken$/);
  assert.match(hosts[2], /:signBlob$/);

  // The OIDC assertion is the subject token, and the audience is the pool.
  assert.equal(stub.calls[0].body.subjectToken, "header.payload.signature");
  assert.equal(stub.calls[0].body.audience, `//iam.googleapis.com/${PROVIDER}`);
});

test("an explicit request token wins over the environment fallback", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    await uploads.signedUploadUrl(GOOD, "video/mp4", undefined, "request.oidc.token");
  }, stub);

  assert.equal(stub.calls[0].body.subjectToken, "request.oidc.token");
  assert.notEqual(stub.calls[0].body.subjectToken, FED_ENV.VERCEL_OIDC_TOKEN);
});

test("the access token is reused rather than re-exchanged per upload", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    await uploads.signedUploadUrl(GOOD, "video/mp4");
    await uploads.signedUploadUrl(GOOD, "video/webm");
  }, stub);

  const exchanges = stub.calls.filter((c) => c.url.includes("sts.googleapis.com"));
  const signings = stub.calls.filter((c) => c.url.includes(":signBlob"));
  assert.equal(exchanges.length, 1, "the token should be cached");
  assert.equal(signings.length, 2, "each URL still needs its own signature");
});

test("no credential or assertion is ever written to the log", async () => {
  const stub = googleStub({ sign: {} }); // forces a failure after the exchange
  await withEnv(FED_ENV, async (logs) => {
    await assert.rejects(() => uploads.signedUploadUrl(GOOD, "video/mp4"));
    for (const line of logs) {
      assert.ok(!line.includes("header.payload.signature"), "OIDC token leaked");
      assert.ok(!line.includes("federated-token"), "federated token leaked");
      assert.ok(!line.includes("sa-access-token"), "access token leaked");
    }
  }, stub);
});

// ── local-key signing still works ────────────────────────────────────

test("the local key path still signs, for development", async () => {
  await withEnv(KEY_ENV, async () => {
    const signed = await uploads.signedUploadUrl(GOOD, "video/mp4");
    assert.match(signed.url, /X-Goog-Signature=[0-9a-f]+$/);
    assert.equal(signed.headers["Content-Type"], "video/mp4");
  });
});

test("content type is a signed header on both paths", async () => {
  await withEnv(KEY_ENV, async () => {
    const signed = await uploads.signedUploadUrl(GOOD, "video/mp4");
    assert.match(signed.url, /X-Goog-SignedHeaders=content-type%3Bhost/);
  });
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    const signed = await uploads.signedUploadUrl(GOOD, "video/mp4");
    assert.match(signed.url, /X-Goog-SignedHeaders=content-type%3Bhost/);
  }, stub);
});

test("an absurd ttl is clamped rather than honoured", async () => {
  await withEnv(KEY_ENV, async () => {
    assert.equal((await uploads.signedUploadUrl(GOOD, "video/mp4", 1)).expiresInSeconds, 60);
    assert.equal(
      (await uploads.signedUploadUrl(GOOD, "video/mp4", 99999999)).expiresInSeconds,
      60 * 60 * 12,
    );
  });
});

test("signing a hostile path throws instead of returning a URL", async () => {
  await withEnv(KEY_ENV, async () => {
    for (const path of HOSTILE) {
      await assert.rejects(() => uploads.signedUploadUrl(path, "video/mp4"));
    }
  });
});

// ── federation misconfiguration is named precisely ───────────────────

test("a missing OIDC token names the setting to enable", async () => {
  const stub = googleStub();
  const env = { ...FED_ENV };
  delete env.VERCEL_OIDC_TOKEN;
  await withEnv(env, async () => {
    await assert.rejects(
      () => uploads.signedUploadUrl(GOOD, "video/mp4"),
      /VERCEL_OIDC_TOKEN/,
    );
  }, stub);
});

test("an OIDC token echoed by Google is redacted from errors and logs", async () => {
  const requestToken = "request.secret.oidc";
  const stub = async () => jsonResponse(
    { error: "invalid_grant", error_description: `rejected ${requestToken}` },
    400,
  );
  await withEnv(FED_ENV, async (logs) => {
    let failure;
    try {
      await uploads.signedUploadUrl(GOOD, "video/mp4", undefined, requestToken);
      assert.fail("should have thrown");
    } catch (err) {
      failure = err;
    }
    assert.ok(!failure.message.includes(requestToken));
    auth.reportFederationFailure(failure);
    assert.ok(logs.every((line) => !line.includes(requestToken)));
    assert.ok(logs.some((line) => line.includes("[redacted]")));
  }, stub);
});

test("a malformed provider resource name is rejected with the expected shape", async () => {
  await withEnv(
    { ...FED_ENV, GCP_WORKLOAD_IDENTITY_PROVIDER: "my-pool" },
    async () => {
      assert.throws(() => auth.federationConfig(), /workloadIdentityPools/);
    },
  );
});

test("a provider set without a service account is refused", async () => {
  await withEnv(
    { GCS_BUCKET: "test-bucket", GCP_WORKLOAD_IDENTITY_PROVIDER: PROVIDER },
    async () => {
      assert.throws(() => auth.federationConfig(), /GCP_SERVICE_ACCOUNT_EMAIL/);
    },
  );
});

test("a non-service-account address is refused", async () => {
  await withEnv(
    { ...FED_ENV, GCP_SERVICE_ACCOUNT_EMAIL: "someone@example.com" },
    async () => {
      assert.throws(() => auth.federationConfig(), /service account address/);
    },
  );
});

test("neither variable set means federation is simply not configured", async () => {
  await withEnv({ GCS_BUCKET: "test-bucket" }, async () => {
    assert.equal(auth.federationConfig(), null);
    assert.equal(auth.federationAvailable(), false);
  });
});

test("a rejected exchange is classed as configuration and logged", async () => {
  const stub = async () =>
    jsonResponse({ error: "invalid_grant", error_description: "audience mismatch" }, 400);
  await withEnv(FED_ENV, async (logs) => {
    await assert.rejects(() => uploads.signedUploadUrl(GOOD, "video/mp4"));
    // The caller logs it; here we assert the classification is available.
    try {
      await uploads.signedUploadUrl(GOOD, "video/mp4");
    } catch (err) {
      assert.equal(err.name, "FederationError");
      assert.equal(err.isConfig, true);
      auth.reportFederationFailure(err);
    }
    assert.ok(logs.some((l) => l.includes("audience mismatch")));
  }, stub);
});

test("a transient google failure is not classed as configuration", async () => {
  const stub = async () => jsonResponse({ error: "backend" }, 503);
  await withEnv(FED_ENV, async () => {
    try {
      await uploads.signedUploadUrl(GOOD, "video/mp4");
      assert.fail("should have thrown");
    } catch (err) {
      assert.equal(err.isConfig, false);
    }
  }, stub);
});

// ── signed reads, keyless too ────────────────────────────────────────

const STORED_SOURCE = `https://storage.googleapis.com/test-bucket/vrf/${USER}/${UPLOAD}/source.mp4`;
const STORED_PROCESSED = `https://storage.googleapis.com/test-bucket/vrf/${USER}/${UPLOAD}/processed.mp4`;
const STORED_UPLOAD = `https://storage.googleapis.com/test-bucket/${GOOD}`;

test("a stored URL is converted back to its object path", async () => {
  await withEnv(KEY_ENV, async () => {
    assert.equal(
      uploads.objectPathFromStored(STORED_PROCESSED),
      `vrf/${USER}/${UPLOAD}/processed.mp4`,
    );
    assert.equal(uploads.objectPathFromStored(STORED_UPLOAD), GOOD);
    // A bare object path is accepted as-is.
    assert.equal(uploads.objectPathFromStored(GOOD), GOOD);
  });
});

test("all three Clipping object shapes can be read", async () => {
  await withEnv(KEY_ENV, async () => {
    for (const stored of [STORED_UPLOAD, STORED_SOURCE, STORED_PROCESSED]) {
      assert.ok(uploads.objectPathFromStored(stored));
    }
  });
});

test("a stored URL for another bucket or host is refused", async () => {
  await withEnv(KEY_ENV, async () => {
    const hostile = [
      `https://storage.googleapis.com/someone-elses-bucket/vrf/${USER}/${UPLOAD}/a.mp4`,
      `https://evil.example.com/test-bucket/vrf/${USER}/${UPLOAD}/a.mp4`,
      "https://storage.googleapis.com/test-bucket/videos/horror/a/b.mp4",
      `https://storage.googleapis.com/test-bucket/vrf/${USER}/${UPLOAD}/../../x.mp4`,
      "not a url at all",
      "",
    ];
    for (const stored of hostile) {
      assert.throws(
        () => uploads.objectPathFromStored(stored),
        `accepted: ${stored}`,
      );
    }
  });
});

test("a read URL is signed keylessly, with no private key present", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    // FED_ENV deliberately contains no GCS_SERVICE_ACCOUNT_JSON.
    assert.equal(process.env.GCS_SERVICE_ACCOUNT_JSON, undefined);
    const signed = await uploads.signedReadUrl(STORED_PROCESSED);
    assert.match(signed.url, /^https:\/\/storage\.googleapis\.com\/test-bucket\//);
    assert.match(signed.url, /X-Goog-Signature=[0-9a-f]+$/);
    assert.ok(signed.url.includes(encodeURIComponent(SA_EMAIL)));
  }, stub);
});

test("a read signs only the host, since a GET has no body", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    const signed = await uploads.signedReadUrl(STORED_PROCESSED);
    assert.match(signed.url, /X-Goog-SignedHeaders=host/);
    assert.ok(!/content-type/.test(signed.url));
  }, stub);
});

test("the method is bound into what gets signed", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    await uploads.signedReadUrl(STORED_UPLOAD);
    await uploads.signedUploadUrl(GOOD, "video/mp4");
  }, stub);

  // The stub returns a fixed signature, so comparing the two URLs' signatures
  // would prove nothing. What matters is that the *strings being signed*
  // differ: if GET and PUT hashed to the same canonical request, a read URL
  // would also authorise a write.
  const payloads = stub.calls
    .filter((c) => c.url.includes(":signBlob"))
    .map((c) => Buffer.from(c.body.payload, "base64").toString());

  assert.equal(payloads.length, 2);
  assert.notEqual(payloads[0], payloads[1]);
});

test("a read URL expires", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    const signed = await uploads.signedReadUrl(STORED_PROCESSED, 300);
    assert.equal(signed.expiresInSeconds, 300);
    assert.match(signed.url, /X-Goog-Expires=300/);
  }, stub);
});

test("reads still work from a local key, for development", async () => {
  await withEnv(KEY_ENV, async () => {
    const signed = await uploads.signedReadUrl(STORED_PROCESSED);
    assert.match(signed.url, /X-Goog-Signature=[0-9a-f]+$/);
  });
});

test("the whole Clipping flow runs with no JSON key anywhere", async () => {
  const stub = googleStub();
  await withEnv(FED_ENV, async () => {
    assert.equal(process.env.GCS_SERVICE_ACCOUNT_JSON, undefined);
    // sign an upload, then read the source, then read the finished clip
    assert.ok((await uploads.signedUploadUrl(GOOD, "video/mp4")).url);
    assert.ok((await uploads.signedReadUrl(STORED_SOURCE)).url);
    assert.ok((await uploads.signedReadUrl(STORED_PROCESSED)).url);
  }, stub);
});

// ── the URL the worker will download ─────────────────────────────────

test("the public URL matches the shape the worker stores and re-reads", async () => {
  await withEnv(KEY_ENV, async () => {
    assert.equal(
      uploads.publicUploadUrl(GOOD),
      `https://storage.googleapis.com/test-bucket/${GOOD}`,
    );
  });
});

test("a hostile path cannot produce a public URL either", async () => {
  await withEnv(KEY_ENV, async () => {
    for (const path of HOSTILE) {
      assert.throws(() => uploads.publicUploadUrl(path));
    }
  });
});
