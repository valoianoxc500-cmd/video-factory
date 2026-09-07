"use strict";

/**
 * Token encryption, including that the Python worker can read what the web
 * app writes.
 *
 * The web app receives OAuth tokens at the callback and the worker spends
 * them at publish time. If the two formats ever diverge, every publish fails
 * with "token failed to decrypt" and the cause is not obvious. The interop
 * test below runs the real Python side rather than a reimplementation of it.
 */

const test = require("node:test");
const assert = require("node:assert");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const {
  encryptToken,
  decryptToken,
  generateTokenKey,
  tokenKeyConfigured,
  TokenSecurityError,
} = require("../.test-build/vrf-crypto.js");

const ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa";
const BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb";

function withKey(key, fn) {
  const previous = process.env.VRF_TOKEN_KEY;
  process.env.VRF_TOKEN_KEY = key;
  try {
    return fn();
  } finally {
    if (previous === undefined) delete process.env.VRF_TOKEN_KEY;
    else process.env.VRF_TOKEN_KEY = previous;
  }
}

test("a token round-trips", () => {
  withKey(generateTokenKey(), () => {
    const sealed = encryptToken("secret-token", ALICE, "youtube");
    assert.notStrictEqual(sealed, "secret-token");
    assert.ok(!sealed.includes("secret-token"));
    assert.strictEqual(decryptToken(sealed, ALICE, "youtube"), "secret-token");
  });
});

test("a ciphertext copied into another user's row will not decrypt", () => {
  withKey(generateTokenKey(), () => {
    const sealed = encryptToken("alice-token", ALICE, "youtube");
    assert.throws(() => decryptToken(sealed, BOB, "youtube"), TokenSecurityError);
  });
});

test("a ciphertext moved to another platform will not decrypt", () => {
  withKey(generateTokenKey(), () => {
    const sealed = encryptToken("alice-token", ALICE, "youtube");
    assert.throws(() => decryptToken(sealed, ALICE, "tiktok"), TokenSecurityError);
  });
});

test("a different key cannot read it", () => {
  const sealed = withKey(generateTokenKey(), () =>
    encryptToken("alice-token", ALICE, "youtube"),
  );
  withKey(generateTokenKey(), () => {
    assert.throws(() => decryptToken(sealed, ALICE, "youtube"), TokenSecurityError);
  });
});

test("tampering is rejected rather than silently wrong", () => {
  withKey(generateTokenKey(), () => {
    const sealed = encryptToken("alice-token", ALICE, "youtube");
    const [version, nonce, body] = sealed.split(".");
    const flipped = body.endsWith("AAAA")
      ? `${body.slice(0, -4)}BBBB`
      : `${body.slice(0, -4)}AAAA`;
    assert.throws(
      () => decryptToken([version, nonce, flipped].join("."), ALICE, "youtube"),
      TokenSecurityError,
    );
  });
});

test("there is no plaintext fallback when no key is configured", () => {
  const previous = process.env.VRF_TOKEN_KEY;
  delete process.env.VRF_TOKEN_KEY;
  try {
    assert.strictEqual(tokenKeyConfigured(), false);
    assert.throws(() => encryptToken("t", ALICE, "youtube"), TokenSecurityError);
  } finally {
    if (previous !== undefined) process.env.VRF_TOKEN_KEY = previous;
  }
});

test("a short key is refused", () => {
  withKey("too-short", () => {
    assert.throws(() => encryptToken("t", ALICE, "youtube"), TokenSecurityError);
  });
});

test("the same token encrypts differently each time", () => {
  withKey(generateTokenKey(), () => {
    const a = encryptToken("t", ALICE, "youtube");
    const b = encryptToken("t", ALICE, "youtube");
    assert.notStrictEqual(a, b);
  });
});

// ── interop with viral/accounts.py ──────────────────────────────────────────

function pythonExecutable() {
  const candidates = [
    path.join(__dirname, "..", "..", ".venv", "Scripts", "python.exe"),
    path.join(__dirname, "..", "..", ".venv", "bin", "python"),
  ];
  return candidates.find((candidate) => fs.existsSync(candidate)) ?? null;
}

function runPython(script, env) {
  const python = pythonExecutable();
  if (!python) return null;
  return execFileSync(python, ["-c", script], {
    cwd: path.join(__dirname, "..", ".."),
    env: { ...process.env, ...env },
    encoding: "utf8",
  }).trim();
}

test("Python decrypts what TypeScript encrypts", { concurrency: false }, (t) => {
  if (!pythonExecutable()) {
    t.skip("no Python virtualenv in this checkout");
    return;
  }
  const key = generateTokenKey();
  const sealed = withKey(key, () => encryptToken("cross-language", ALICE, "youtube"));

  const out = runPython(
    "from viral.accounts import TokenCipher;" +
      "import os;" +
      "print(TokenCipher().decrypt(os.environ['SEALED']," +
      " user_id=os.environ['UID'], platform='youtube'))",
    { VRF_TOKEN_KEY: key, SEALED: sealed, UID: ALICE },
  );
  assert.strictEqual(out, "cross-language");
});

test("TypeScript decrypts what Python encrypts", { concurrency: false }, (t) => {
  if (!pythonExecutable()) {
    t.skip("no Python virtualenv in this checkout");
    return;
  }
  const key = generateTokenKey();
  const sealed = runPython(
    "from viral.accounts import TokenCipher;" +
      "import os;" +
      "print(TokenCipher().encrypt('from-python'," +
      " user_id=os.environ['UID'], platform='tiktok'))",
    { VRF_TOKEN_KEY: key, UID: ALICE },
  );

  withKey(key, () => {
    assert.strictEqual(decryptToken(sealed, ALICE, "tiktok"), "from-python");
    // And the owner binding survives the crossing.
    assert.throws(() => decryptToken(sealed, BOB, "tiktok"), TokenSecurityError);
  });
});
