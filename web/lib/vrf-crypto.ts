/**
 * OAuth token encryption, wire-compatible with viral/accounts.py.
 *
 * The web app receives tokens at the OAuth callback and the Python worker
 * spends them at publish time, so both ends must agree on the format exactly:
 *
 *   v1.<base64 12-byte nonce>.<base64 (ciphertext || 16-byte GCM tag)>
 *
 * with AES-256-GCM and the associated data `"<user_id>|<platform>"`. Binding
 * the owner and platform into the AAD rather than the payload means a
 * ciphertext copied into another user's row fails to authenticate instead of
 * quietly decrypting -- the same second lock the Python side describes.
 *
 * There is deliberately no plaintext fallback. Without VRF_TOKEN_KEY the
 * connect flow refuses to store anything.
 */

import { createCipheriv, createDecipheriv, randomBytes } from "node:crypto";

const VERSION = "v1";
const NONCE_BYTES = 12;
const TAG_BYTES = 16;

export class TokenSecurityError extends Error {
  readonly status = 500;
  constructor(message: string) {
    super(message);
    this.name = "TokenSecurityError";
  }
}

function key(): Buffer {
  const raw = (process.env.VRF_TOKEN_KEY ?? "").trim();
  if (!raw) {
    throw new TokenSecurityError(
      "VRF_TOKEN_KEY is not set. OAuth tokens are never stored " +
        "unencrypted, so connecting an account is disabled until a key is " +
        "configured.",
    );
  }
  let decoded: Buffer;
  try {
    decoded = Buffer.from(raw, "base64");
  } catch {
    decoded = Buffer.from(raw, "utf8");
  }
  // A short base64 string decodes silently, so the length check is the real
  // guard rather than the try/catch above.
  if (decoded.length !== 32) decoded = Buffer.from(raw, "utf8");
  if (decoded.length !== 32) {
    throw new TokenSecurityError(
      `VRF_TOKEN_KEY must decode to 32 bytes (got ${decoded.length}).`,
    );
  }
  return decoded;
}

export function tokenKeyConfigured(): boolean {
  try {
    key();
    return true;
  } catch {
    return false;
  }
}

function aad(userId: string, platform: string): Buffer {
  return Buffer.from(`${userId.trim()}|${platform.trim().toLowerCase()}`, "utf8");
}

export function encryptToken(
  plaintext: string,
  userId: string,
  platform: string,
): string {
  if (!plaintext) return "";
  const nonce = randomBytes(NONCE_BYTES);
  const cipher = createCipheriv("aes-256-gcm", key(), nonce);
  cipher.setAAD(aad(userId, platform));
  const sealed = Buffer.concat([
    cipher.update(plaintext, "utf8"),
    cipher.final(),
    cipher.getAuthTag(),
  ]);
  return `${VERSION}.${nonce.toString("base64")}.${sealed.toString("base64")}`;
}

export function decryptToken(
  ciphertext: string,
  userId: string,
  platform: string,
): string {
  if (!ciphertext) return "";
  try {
    const [version, nonceB64, sealedB64] = ciphertext.split(".", 3);
    if (version !== VERSION) throw new Error("unknown ciphertext version");
    const sealed = Buffer.from(sealedB64, "base64");
    const body = sealed.subarray(0, sealed.length - TAG_BYTES);
    const tag = sealed.subarray(sealed.length - TAG_BYTES);

    const decipher = createDecipheriv(
      "aes-256-gcm",
      key(),
      Buffer.from(nonceB64, "base64"),
    );
    decipher.setAAD(aad(userId, platform));
    decipher.setAuthTag(tag);
    return Buffer.concat([decipher.update(body), decipher.final()]).toString("utf8");
  } catch (err) {
    if (err instanceof TokenSecurityError) throw err;
    // The message never carries the ciphertext or the key.
    throw new TokenSecurityError(
      "Stored token failed to decrypt. It was encrypted for a different " +
        "user, platform or key.",
    );
  }
}

/** A fresh key, for an operator setting the environment up. */
export function generateTokenKey(): string {
  return randomBytes(32).toString("base64");
}
