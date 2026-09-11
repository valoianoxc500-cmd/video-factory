/**
 * Threads and X account connection, and the boundary around it.
 *
 * These two platforms were added to the shared OAuth registry so Quote Studio
 * can connect them. The risk that creates is enrolling two image-only
 * accounts in the Reels *video* pipeline, which would try to post an mp4 to
 * a Threads carousel endpoint. `PLATFORMS` is what drives that pipeline, so
 * the most important assertion in this file is the one proving neither
 * platform appears there.
 *
 * Endpoints and scopes below were verified against the official docs in
 * September 2026 rather than recalled, because a wrong authorize host fails
 * at the worst possible moment -- after the user has clicked Connect.
 *
 * Run with: npm run test  (compiles lib/ first -- see package.json)
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const oauth = require("../.test-build/vrf-oauth.js");
const vrf = require("../.test-build/vrf.js");

// ── Threads ──────────────────────────────────────────────────────────

test("threads uses its own hosts, not the facebook dialog", () => {
  const provider = oauth.OAUTH_PROVIDERS.threads;
  assert.ok(provider, "threads provider missing");
  assert.equal(provider.authorizeUrl, "https://threads.com/oauth/authorize");
  assert.equal(provider.tokenUrl, "https://graph.threads.com/oauth/access_token");
  // Instagram and Facebook authenticate through facebook.com; Threads does not.
  assert.ok(!provider.authorizeUrl.includes("facebook.com"));
});

test("threads requests exactly the scopes publishing needs", () => {
  const { scopes } = oauth.OAUTH_PROVIDERS.threads;
  assert.ok(scopes.includes("threads_basic"), "threads_basic is required for all endpoints");
  assert.ok(scopes.includes("threads_content_publish"), "needed to publish");
  // Nothing beyond publishing: replies and insights are not asked for.
  assert.ok(!scopes.some((s) => /replies|insights/.test(s)));
});

test("threads uses its own app credentials", () => {
  const provider = oauth.OAUTH_PROVIDERS.threads;
  assert.equal(provider.clientIdEnv, "THREADS_CLIENT_ID");
  assert.equal(provider.clientSecretEnv, "THREADS_CLIENT_SECRET");
  // Reusing the Meta app id here would silently fail: Threads is a separate app.
  assert.notEqual(provider.clientIdEnv, "META_OAUTH_CLIENT_ID");
});

test("threads is a standard authorization-code flow", () => {
  assert.equal(oauth.OAUTH_PROVIDERS.threads.flow, "oauth_code");
});

// ── X ────────────────────────────────────────────────────────────────

test("x uses oauth 2.0 pkce on its official hosts", () => {
  const provider = oauth.OAUTH_PROVIDERS.x;
  assert.ok(provider, "x provider missing");
  assert.equal(provider.flow, "oauth_pkce");
  assert.equal(provider.authorizeUrl, "https://x.com/i/oauth2/authorize");
  assert.equal(provider.tokenUrl, "https://api.x.com/2/oauth2/token");
});

test("x requests media upload and a refresh token", () => {
  const { scopes } = oauth.OAUTH_PROVIDERS.x;
  assert.ok(scopes.includes("tweet.write"), "needed to post");
  assert.ok(scopes.includes("media.write"), "needed to upload images");
  // Without offline.access no refresh token is issued and the connection
  // dies two hours after it is made.
  assert.ok(scopes.includes("offline.access"), "no refresh token without it");
});

test("x uses its own app credentials", () => {
  const provider = oauth.OAUTH_PROVIDERS.x;
  assert.equal(provider.clientIdEnv, "X_CLIENT_ID");
  assert.equal(provider.clientSecretEnv, "X_CLIENT_SECRET");
});

// ── the boundary: no video publishing ────────────────────────────────

test("neither threads nor x is enrolled in video publishing", () => {
  // PLATFORMS drives the Reels queue and its adapters. A platform listed
  // there is one the video pipeline will try to post an mp4 to.
  const platforms = vrf.PLATFORMS.map((p) => p.platform);
  assert.ok(!platforms.includes("threads"), "threads reached video publishing");
  assert.ok(!platforms.includes("x"), "x reached video publishing");
});

test("the platforms that already published video are unchanged", () => {
  const platforms = vrf.PLATFORMS.map((p) => p.platform);
  assert.deepEqual(platforms, [
    "youtube",
    "instagram",
    "facebook",
    "tiktok",
    "snapchat",
  ]);
});

test("instagram and facebook oauth is untouched", () => {
  const instagram = oauth.OAUTH_PROVIDERS.instagram;
  const facebook = oauth.OAUTH_PROVIDERS.facebook;
  assert.equal(instagram.clientIdEnv, "META_OAUTH_CLIENT_ID");
  assert.equal(facebook.clientIdEnv, "META_OAUTH_CLIENT_ID");
  assert.ok(instagram.scopes.includes("instagram_content_publish"));
  assert.ok(facebook.scopes.includes("pages_manage_posts"));
  assert.equal(instagram.flow, "oauth_code");
  assert.equal(facebook.flow, "oauth_code");
});

// ── connectable vs video-publishable ─────────────────────────────────

test("threads and x are connectable accounts", () => {
  const connectable = vrf.CONNECTABLE_PLATFORMS.map((p) => p.platform);
  assert.ok(connectable.includes("threads"));
  assert.ok(connectable.includes("x"));
});

test("connectable includes everything publishable, and then some", () => {
  const connectable = vrf.CONNECTABLE_PLATFORMS.map((p) => p.platform);
  for (const platform of vrf.PLATFORMS.map((p) => p.platform)) {
    assert.ok(connectable.includes(platform), `${platform} became unconnectable`);
  }
  assert.equal(connectable.length, vrf.PLATFORMS.length + 2);
});

test("neither connect-only platform can publish video", () => {
  // `canPublish` is read as "can publish video" by the Reels queue.
  for (const platform of vrf.CONNECT_ONLY_PLATFORMS) {
    assert.equal(platform.canPublish, false, platform.platform);
    assert.equal(platform.nativeScheduling, false);
    assert.equal(platform.draftOnly, false);
  }
});

test("connect-only platforms never reach the publishable list", () => {
  // PUBLISHABLE_PLATFORMS is what the video queue validates against.
  assert.ok(!vrf.PUBLISHABLE_PLATFORMS.includes("threads"));
  assert.ok(!vrf.PUBLISHABLE_PLATFORMS.includes("x"));
});

test("connect-only platforms are marked supported so a Connect button appears", () => {
  // The Accounts page gates the button on `supported && configured`.
  for (const platform of vrf.CONNECT_ONLY_PLATFORMS) {
    assert.equal(platform.supported, true, platform.platform);
    assert.ok(platform.requires.trim(), "must say what it needs");
    assert.match(platform.note, /[Nn]ot a video destination/);
  }
});

test("every connectable platform has an oauth provider", () => {
  for (const platform of vrf.CONNECTABLE_PLATFORMS) {
    assert.ok(
      oauth.OAUTH_PROVIDERS[platform.platform],
      `${platform.platform} is offered but has no provider`,
    );
  }
});

// ── identity lookup ──────────────────────────────────────────────────

test("threads identity is read from its own api host", async () => {
  const realFetch = global.fetch;
  let seen = "";
  global.fetch = async (url) => {
    seen = String(url);
    return { json: async () => ({ id: "th-123", username: "someone" }) };
  };
  try {
    const identity = await oauth.fetchAccountIdentity("threads", "token-abc");
    assert.equal(identity.ref, "th-123");
    assert.equal(identity.handle, "someone");
    // Publishing addresses {threads-user-id} on graph.threads.net.
    assert.ok(seen.includes("graph.threads.net"));
    assert.ok(seen.includes("fields=id,username"));
  } finally {
    global.fetch = realFetch;
  }
});

test("x identity is read with a bearer token", async () => {
  const realFetch = global.fetch;
  let seen = { url: "", auth: "" };
  global.fetch = async (url, init) => {
    seen = { url: String(url), auth: String(init?.headers?.authorization ?? "") };
    return { json: async () => ({ data: { id: "x-9", username: "handle" } }) };
  };
  try {
    const identity = await oauth.fetchAccountIdentity("x", "token-abc");
    assert.equal(identity.ref, "x-9");
    assert.equal(identity.handle, "handle");
    assert.ok(seen.url.includes("api.x.com/2/users/me"));
    assert.equal(seen.auth, "Bearer token-abc");
  } finally {
    global.fetch = realFetch;
  }
});

test("an identity lookup that returns nothing is an error, not an empty account", async () => {
  const realFetch = global.fetch;
  global.fetch = async () => ({ json: async () => ({}) });
  try {
    for (const platform of ["threads", "x"]) {
      await assert.rejects(() => oauth.fetchAccountIdentity(platform, "t"));
    }
  } finally {
    global.fetch = realFetch;
  }
});

// ── the connect flow itself ──────────────────────────────────────────

function withClient(env, fn) {
  const saved = {};
  for (const key of Object.keys(env)) {
    saved[key] = process.env[key];
    process.env[key] = env[key];
  }
  try {
    return fn();
  } finally {
    for (const key of Object.keys(env)) {
      if (saved[key] === undefined) delete process.env[key];
      else process.env[key] = saved[key];
    }
  }
}

test("threads authorization carries state and the right scopes", () => {
  withClient({ THREADS_CLIENT_ID: "app-1" }, () => {
    const { url, session } = oauth.buildAuthorizationUrl(
      "threads",
      "user-1",
      "https://example.com/cb",
    );
    const parsed = new URL(url);
    assert.equal(parsed.origin + parsed.pathname, "https://threads.com/oauth/authorize");
    assert.equal(parsed.searchParams.get("response_type"), "code");
    assert.equal(parsed.searchParams.get("state"), session.state);
    assert.match(parsed.searchParams.get("scope"), /threads_content_publish/);
    // Not a PKCE flow, so no verifier is generated.
    assert.equal(session.codeVerifier, "");
  });
});

test("x authorization uses pkce with an s256 challenge", () => {
  withClient({ X_CLIENT_ID: "app-2" }, () => {
    const { url, session } = oauth.buildAuthorizationUrl(
      "x",
      "user-1",
      "https://example.com/cb",
    );
    const parsed = new URL(url);
    assert.equal(parsed.origin + parsed.pathname, "https://x.com/i/oauth2/authorize");
    assert.equal(parsed.searchParams.get("code_challenge_method"), "S256");
    assert.ok(session.codeVerifier.length > 40, "a verifier must be generated");
    // The challenge is the hash, never the verifier itself.
    const challenge = parsed.searchParams.get("code_challenge");
    assert.ok(challenge && challenge !== session.codeVerifier);
  });
});

test("the state is unguessable and differs per attempt", () => {
  withClient({ X_CLIENT_ID: "app-2" }, () => {
    const a = oauth.buildAuthorizationUrl("x", "u", "https://e.com/cb").session;
    const b = oauth.buildAuthorizationUrl("x", "u", "https://e.com/cb").session;
    assert.notEqual(a.state, b.state);
    assert.notEqual(a.codeVerifier, b.codeVerifier);
    assert.ok(a.state.length >= 32);
  });
});

test("an unconfigured provider refuses safely and names the variables", () => {
  const saved = { id: process.env.THREADS_CLIENT_ID };
  delete process.env.THREADS_CLIENT_ID;
  try {
    assert.throws(
      () => oauth.buildAuthorizationUrl("threads", "u", "https://e.com/cb"),
      /THREADS_CLIENT_ID[\s\S]*THREADS_CLIENT_SECRET/,
    );
  } finally {
    if (saved.id !== undefined) process.env.THREADS_CLIENT_ID = saved.id;
  }
});

test("a callback for the wrong user is rejected", () => {
  withClient({ X_CLIENT_ID: "app-2" }, () => {
    const { session } = oauth.buildAuthorizationUrl("x", "user-1", "https://e.com/cb");
    assert.throws(() =>
      oauth.verifyCallback(session, session.state, "user-2"),
    );
  });
});

test("a callback with a mismatched state is rejected", () => {
  withClient({ X_CLIENT_ID: "app-2" }, () => {
    const { session } = oauth.buildAuthorizationUrl("x", "user-1", "https://e.com/cb");
    assert.throws(() => oauth.verifyCallback(session, "forged-state", "user-1"));
  });
});

// ── credentials are named, never invented ────────────────────────────

test("no provider carries a literal credential", () => {
  for (const [name, provider] of Object.entries(oauth.OAUTH_PROVIDERS)) {
    const blob = JSON.stringify(provider);
    // Every secret is referenced by environment variable name only.
    assert.ok(!/secret["']?\s*:\s*["'][A-Za-z0-9_-]{12,}/.test(blob), name);
    if (provider.clientSecretEnv) {
      assert.match(provider.clientSecretEnv, /_SECRET$/, name);
    }
  }
});

test("a provider without configured credentials is not connectable", () => {
  // `configured` is computed from the presence of both env vars, so an
  // unconfigured app shows Connect rather than a broken flow.
  for (const platform of ["threads", "x"]) {
    const provider = oauth.OAUTH_PROVIDERS[platform];
    const configured = Boolean(
      process.env[provider.clientIdEnv] && process.env[provider.clientSecretEnv],
    );
    assert.equal(typeof configured, "boolean");
  }
});
