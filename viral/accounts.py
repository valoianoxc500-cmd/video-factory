"""Connected social accounts: OAuth, encrypted tokens, per-user isolation.

Three rules this module exists to enforce
-----------------------------------------
1. **Never a password.** Accounts are connected through each platform's own
   OAuth consent screen. This codebase has no field, parameter or storage for a
   social password, and `reject_password_credentials` refuses a payload that
   carries one rather than quietly ignoring it.

2. **Tokens are never stored in plaintext.** `TokenCipher` is AES-256-GCM and
   refuses to operate without a key -- there is deliberately no "store it plain
   if the key is missing" path, because that fallback is how token stores leak.
   Each ciphertext is bound to its owner and platform through the AEAD's
   associated data, so a row copied into another user's account, or moved to a
   different platform's row, fails to decrypt instead of silently working.

3. **A user only ever reaches their own accounts.** Every store method takes
   the acting user's id and filters on it; there is no unscoped read. Asking
   for an account by id that belongs to someone else raises
   `AccountIsolationError` rather than returning None, because a silent None
   hides an attempted cross-tenant read that is worth seeing in the logs.

The database enforces the same thing independently: `vrf_accounts` has RLS
with `auth.uid() = user_id`. This module is the second lock, not the only one.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable, Iterable

logger = logging.getLogger("viral.accounts")


class AccountError(Exception):
    """A problem connecting or using a social account."""


class AccountIsolationError(AccountError):
    """An operation reached for an account belonging to a different user."""


class TokenSecurityError(AccountError):
    """Token encryption is unavailable or the ciphertext failed to verify."""


# ---------------------------------------------------------------------------
# Never passwords
# ---------------------------------------------------------------------------

#: Field names that indicate someone is trying to hand us account credentials.
_CREDENTIAL_FIELDS = frozenset({
    "password", "passwd", "pass", "pwd", "passphrase",
    "login_password", "account_password", "social_password",
    "otp", "one_time_password", "two_factor_code", "2fa_code",
    "security_answer", "pin",
})


def reject_password_credentials(payload: dict) -> None:
    """Refuse a connect request that carries a platform password.

    Connection happens on the platform's own consent screen. A password
    arriving here means either a mis-built client or a phishing-shaped flow,
    and both should stop loudly.
    """
    offending = sorted(
        key for key in (payload or {})
        if str(key).strip().lower().replace("-", "_") in _CREDENTIAL_FIELDS
    )
    if offending:
        raise AccountError(
            f"Refusing credentials in fields {offending}. Accounts are "
            f"connected through the platform's OAuth consent screen; this "
            f"application never asks for, receives or stores a social "
            f"account password."
        )


# ---------------------------------------------------------------------------
# Token encryption
# ---------------------------------------------------------------------------

#: Where the 32-byte AES key lives, base64-encoded. Server-side only.
TOKEN_KEY_ENV = "VRF_TOKEN_KEY"
_CIPHER_PREFIX = "v1"


def generate_token_key() -> str:
    """A fresh base64 key, for operators setting the environment up."""
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


class TokenCipher:
    """AES-256-GCM for OAuth tokens at rest.

    `context` is the owner and platform. It goes into the AEAD's associated
    data rather than the ciphertext, so it is authenticated but not stored
    twice, and a ciphertext lifted into another user's row will not decrypt.
    """

    def __init__(self, key: bytes | str | None = None) -> None:
        raw = key if key is not None else os.environ.get(TOKEN_KEY_ENV, "")
        self._key = self._normalise(raw)

    @staticmethod
    def _normalise(raw: bytes | str) -> bytes:
        if isinstance(raw, bytes):
            data = raw
        else:
            text = str(raw or "").strip()
            if not text:
                raise TokenSecurityError(
                    f"{TOKEN_KEY_ENV} is not set. OAuth tokens are never "
                    f"stored unencrypted, so account connection is disabled "
                    f"until a key is configured."
                )
            try:
                data = base64.b64decode(text, validate=True)
            except Exception:
                data = text.encode("utf-8")
        if len(data) != 32:
            raise TokenSecurityError(
                f"{TOKEN_KEY_ENV} must decode to 32 bytes (got {len(data)}). "
                f"Generate one with viral.accounts.generate_token_key()."
            )
        return data

    @staticmethod
    def available() -> bool:
        try:
            TokenCipher()
            return True
        except TokenSecurityError:
            return False

    def _aead(self):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(self._key)

    @staticmethod
    def _context(user_id: str, platform: str) -> bytes:
        return f"{str(user_id).strip()}|{str(platform).strip().lower()}".encode()

    def encrypt(self, plaintext: str, *, user_id: str, platform: str) -> str:
        if plaintext is None or plaintext == "":
            return ""
        nonce = secrets.token_bytes(12)
        sealed = self._aead().encrypt(
            nonce, str(plaintext).encode("utf-8"),
            self._context(user_id, platform),
        )
        return ".".join((
            _CIPHER_PREFIX,
            base64.b64encode(nonce).decode("ascii"),
            base64.b64encode(sealed).decode("ascii"),
        ))

    def decrypt(self, ciphertext: str, *, user_id: str, platform: str) -> str:
        if not ciphertext:
            return ""
        try:
            version, nonce_b64, sealed_b64 = str(ciphertext).split(".", 2)
            if version != _CIPHER_PREFIX:
                raise ValueError(f"unknown ciphertext version {version!r}")
            plain = self._aead().decrypt(
                base64.b64decode(nonce_b64),
                base64.b64decode(sealed_b64),
                self._context(user_id, platform),
            )
        except Exception as exc:
            # The message never carries the ciphertext or the key.
            raise TokenSecurityError(
                "Stored token failed to decrypt. It was encrypted for a "
                "different user, platform or key."
            ) from exc
        return plain.decode("utf-8")


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------

class ConnectFlow(str, Enum):
    OAUTH_PKCE = "oauth_pkce"
    OAUTH_CODE = "oauth_code"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class OAuthProvider:
    platform: str
    flow: ConnectFlow
    authorize_url: str = ""
    token_url: str = ""
    scopes: tuple[str, ...] = ()
    client_id_env: str = ""
    client_secret_env: str = ""
    note: str = ""

    @property
    def supported(self) -> bool:
        return self.flow is not ConnectFlow.UNSUPPORTED

    def configured(self) -> bool:
        return bool(
            self.supported
            and os.environ.get(self.client_id_env, "")
            and os.environ.get(self.client_secret_env, "")
        )


OAUTH_PROVIDERS: dict[str, OAuthProvider] = {
    "youtube": OAuthProvider(
        "youtube", ConnectFlow.OAUTH_CODE,
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        scopes=("https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube.readonly"),
        client_id_env="YOUTUBE_OAUTH_CLIENT_ID",
        client_secret_env="YOUTUBE_OAUTH_CLIENT_SECRET",
    ),
    "instagram": OAuthProvider(
        "instagram", ConnectFlow.OAUTH_CODE,
        authorize_url="https://www.facebook.com/v21.0/dialog/oauth",
        token_url="https://graph.facebook.com/v21.0/oauth/access_token",
        scopes=("instagram_basic", "instagram_content_publish",
                "pages_show_list", "business_management"),
        client_id_env="META_OAUTH_CLIENT_ID",
        client_secret_env="META_OAUTH_CLIENT_SECRET",
        note="Requires an Instagram Business or Creator account linked to a Page.",
    ),
    "facebook": OAuthProvider(
        "facebook", ConnectFlow.OAUTH_CODE,
        authorize_url="https://www.facebook.com/v21.0/dialog/oauth",
        token_url="https://graph.facebook.com/v21.0/oauth/access_token",
        scopes=("pages_show_list", "pages_manage_posts", "pages_read_engagement"),
        client_id_env="META_OAUTH_CLIENT_ID",
        client_secret_env="META_OAUTH_CLIENT_SECRET",
    ),
    "tiktok": OAuthProvider(
        "tiktok", ConnectFlow.OAUTH_PKCE,
        authorize_url="https://www.tiktok.com/v2/auth/authorize/",
        token_url="https://open.tiktokapis.com/v2/oauth/token/",
        scopes=("user.info.basic", "video.publish", "video.upload"),
        client_id_env="TIKTOK_CLIENT_KEY",
        client_secret_env="TIKTOK_CLIENT_SECRET",
        note="video.publish requires an audited app; unaudited apps post to drafts.",
    ),
    # Threads and X are connected for Quote Studio's carousel publishing.
    # They are deliberately absent from the publishing PLATFORMS table, which
    # is what the Reels video queue iterates -- an image-only account enrolled
    # there would be handed an mp4.
    "threads": OAuthProvider(
        "threads", ConnectFlow.OAUTH_CODE,
        # Threads authenticates on its own hosts, separate from the Facebook
        # dialog Instagram and Facebook share.
        authorize_url="https://threads.com/oauth/authorize",
        token_url="https://graph.threads.com/oauth/access_token",
        scopes=("threads_basic", "threads_content_publish"),
        client_id_env="THREADS_CLIENT_ID",
        client_secret_env="THREADS_CLIENT_SECRET",
        note=(
            "Needs its own Meta Threads app -- the Instagram/Facebook app ID "
            "does not work here."
        ),
    ),
    "x": OAuthProvider(
        "x", ConnectFlow.OAUTH_PKCE,
        authorize_url="https://x.com/i/oauth2/authorize",
        token_url="https://api.x.com/2/oauth2/token",
        # media.write uploads the images; without offline.access no refresh
        # token is issued and the connection dies after two hours.
        scopes=("tweet.read", "tweet.write", "media.write",
                "users.read", "offline.access"),
        client_id_env="X_CLIENT_ID",
        client_secret_env="X_CLIENT_SECRET",
        note="X carries at most 4 images per post, so it has no true carousel.",
    ),
    "snapchat": OAuthProvider(
        "snapchat", ConnectFlow.UNSUPPORTED,
        note=(
            "Snapchat has no server-side publishing API, so connecting an "
            "account would not enable anything this product can do."
        ),
    ),
}


@dataclass(frozen=True)
class AuthSession:
    """The short-lived state of one in-flight connection attempt.

    `state` is bound to the user who started the flow. The callback proves it
    is the same person finishing it, which is what stops one user's callback
    from attaching a social account to another user's row.
    """

    platform: str
    user_id: str
    state: str
    redirect_uri: str
    code_verifier: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def expired(self, *, now: datetime | None = None, ttl_minutes: int = 15) -> bool:
        reference = now or datetime.now(timezone.utc)
        return reference > self.created_at + timedelta(minutes=ttl_minutes)


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode().rstrip("=")
    return verifier, challenge


def build_authorization_url(
    platform: str,
    user_id: str,
    redirect_uri: str,
    *,
    client_id: str | None = None,
    now: datetime | None = None,
) -> tuple[str, AuthSession]:
    """The consent-screen URL to send the user to, and the state to keep."""
    from urllib.parse import urlencode

    key = str(platform or "").strip().lower()
    provider = OAUTH_PROVIDERS.get(key)
    if provider is None:
        raise AccountError(f"Unknown platform {platform!r}.")
    if not provider.supported:
        raise AccountError(f"{key}: {provider.note}")
    if not str(user_id).strip():
        raise AccountError("Connecting an account requires a signed-in user.")
    if not str(redirect_uri).strip():
        raise AccountError("A redirect URI is required.")

    resolved_id = client_id or os.environ.get(provider.client_id_env, "")
    if not resolved_id:
        raise AccountError(
            f"{key} is not configured. Set {provider.client_id_env} and "
            f"{provider.client_secret_env}."
        )

    verifier, challenge = ("", "")
    if provider.flow is ConnectFlow.OAUTH_PKCE:
        verifier, challenge = _pkce_pair()

    session = AuthSession(
        platform=key,
        user_id=str(user_id),
        state=secrets.token_urlsafe(32),
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        created_at=now or datetime.now(timezone.utc),
    )

    params = {
        "client_id": resolved_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(provider.scopes),
        "state": session.state,
    }
    if key == "youtube":
        # Without these Google returns no refresh token on reconnects.
        params["access_type"] = "offline"
        params["prompt"] = "consent"
    if challenge:
        params["code_challenge"] = challenge
        params["code_challenge_method"] = "S256"
    return f"{provider.authorize_url}?{urlencode(params)}", session


def verify_callback(
    session: AuthSession | None,
    *,
    returned_state: str,
    acting_user_id: str,
    now: datetime | None = None,
) -> AuthSession:
    """Check that this callback belongs to this user's own flow."""
    if session is None:
        raise AccountError("No connection is in progress. Start again.")
    if not hmac.compare_digest(str(session.state), str(returned_state or "")):
        raise AccountError("OAuth state did not match. Connection refused.")
    if str(session.user_id) != str(acting_user_id):
        raise AccountIsolationError(
            "This connection was started by a different user; refusing to "
            "attach the account."
        )
    if session.expired(now=now):
        raise AccountError("The connection attempt expired. Start again.")
    return session


# ---------------------------------------------------------------------------
# Stored accounts
# ---------------------------------------------------------------------------

@dataclass
class ConnectedAccount:
    """A connected account as it is stored. Tokens are ciphertext here."""

    id: str
    user_id: str
    platform: str
    account_handle: str = ""
    account_ref: str = ""
    scopes: str = ""
    access_token_encrypted: str = ""
    refresh_token_encrypted: str = ""
    token_expires_at: datetime | None = None
    connected_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    def expires_within(self, seconds: float, *, now: datetime | None = None) -> bool:
        if self.token_expires_at is None:
            return False
        reference = now or datetime.now(timezone.utc)
        return self.token_expires_at <= reference + timedelta(seconds=seconds)

    def to_record(self) -> dict:
        """What the UI is allowed to see. No token material, encrypted or not."""
        return {
            "id": self.id,
            "platform": self.platform,
            "account_handle": self.account_handle,
            "connected_at": self.connected_at.isoformat() if self.connected_at else None,
            "expires_at": (
                self.token_expires_at.isoformat() if self.token_expires_at else None
            ),
            "active": self.active,
        }

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return (
            f"ConnectedAccount(id={self.id!r}, user_id={self.user_id!r}, "
            f"platform={self.platform!r}, tokens=<redacted>)"
        )


class AccountStore:
    """In-process account storage, scoped by user on every call.

    Mirrors the shape the Supabase-backed store uses so the isolation rules
    are testable without a database. `user_id` is a required argument on every
    method by design: there is no way to express "all accounts" here.
    """

    def __init__(self) -> None:
        self._rows: dict[str, ConnectedAccount] = {}

    def save(self, account: ConnectedAccount) -> ConnectedAccount:
        if not str(account.user_id).strip():
            raise AccountIsolationError("An account must belong to a user.")
        if account.connected_at is None:
            account.connected_at = datetime.now(timezone.utc)
        self._rows[account.id] = account
        return account

    def list_for_user(self, user_id: str, *, include_revoked: bool = False) -> list[ConnectedAccount]:
        return [
            row for row in self._rows.values()
            if row.user_id == user_id and (include_revoked or row.active)
        ]

    def get(self, user_id: str, platform: str) -> ConnectedAccount | None:
        key = str(platform or "").strip().lower()
        for row in self._rows.values():
            if row.user_id == user_id and row.platform == key and row.active:
                return row
        return None

    def get_by_id(self, user_id: str, account_id: str) -> ConnectedAccount:
        row = self._rows.get(account_id)
        if row is None:
            raise AccountError(f"No account {account_id!r}.")
        if row.user_id != user_id:
            logger.warning(
                f"cross-user account read refused: user={user_id} "
                f"account={account_id}"
            )
            raise AccountIsolationError(
                "That account belongs to a different user."
            )
        return row

    def revoke(self, user_id: str, account_id: str, *, now: datetime | None = None) -> ConnectedAccount:
        row = self.get_by_id(user_id, account_id)     # raises on cross-user
        row.revoked_at = now or datetime.now(timezone.utc)
        # Revocation clears the token material; a revoked row keeps only the
        # audit fields, so a leaked backup cannot be replayed.
        row.access_token_encrypted = ""
        row.refresh_token_encrypted = ""
        return row

    def delete(self, user_id: str, account_id: str) -> None:
        self.get_by_id(user_id, account_id)
        self._rows.pop(account_id, None)


def connect_account(
    store: AccountStore,
    cipher: TokenCipher,
    *,
    user_id: str,
    platform: str,
    account_handle: str,
    account_ref: str,
    access_token: str,
    refresh_token: str = "",
    expires_in: float | None = None,
    scopes: Iterable[str] = (),
    account_id: str | None = None,
    now: datetime | None = None,
) -> ConnectedAccount:
    """Store a freshly authorised account, tokens encrypted to this user."""
    key = str(platform or "").strip().lower()
    if key not in OAUTH_PROVIDERS:
        raise AccountError(f"Unknown platform {platform!r}.")
    if not str(user_id).strip():
        raise AccountIsolationError("An account must belong to a user.")
    if not str(access_token).strip():
        raise AccountError("The platform returned no access token.")

    reference = now or datetime.now(timezone.utc)
    expires_at = (
        reference + timedelta(seconds=float(expires_in)) if expires_in else None
    )
    return store.save(ConnectedAccount(
        id=account_id or secrets.token_hex(16),
        user_id=str(user_id),
        platform=key,
        account_handle=account_handle,
        account_ref=account_ref,
        scopes=" ".join(scopes),
        access_token_encrypted=cipher.encrypt(
            access_token, user_id=user_id, platform=key),
        refresh_token_encrypted=cipher.encrypt(
            refresh_token, user_id=user_id, platform=key) if refresh_token else "",
        token_expires_at=expires_at,
        connected_at=reference,
    ))


#: Refresh this far ahead of expiry, so a publish never starts on a token that
#: dies mid-upload.
REFRESH_MARGIN_SECONDS = 300.0


def resolve_access_token(
    store: AccountStore,
    cipher: TokenCipher,
    *,
    user_id: str,
    platform: str,
    refresher: Callable[[str, str], dict] | None = None,
    now: datetime | None = None,
) -> str:
    """The usable access token for this user's account on this platform.

    Decrypts on demand and refreshes when close to expiry. The plaintext token
    only exists for the duration of the caller's request.
    """
    account = store.get(user_id, platform)
    if account is None:
        raise AccountError(
            f"No connected {platform} account. Connect one in Settings."
        )
    if not account.active:
        raise AccountError(f"The {platform} connection was revoked.")

    reference = now or datetime.now(timezone.utc)
    if account.expires_within(REFRESH_MARGIN_SECONDS, now=reference):
        if refresher is None or not account.refresh_token_encrypted:
            raise AccountError(
                f"The {platform} token expired and cannot be refreshed. "
                f"Reconnect the account."
            )
        refresh_token = cipher.decrypt(
            account.refresh_token_encrypted,
            user_id=account.user_id, platform=account.platform,
        )
        payload = refresher(account.platform, refresh_token) or {}
        new_access = str(payload.get("access_token") or "")
        if not new_access:
            raise AccountError(
                f"Refreshing the {platform} token failed. Reconnect the account."
            )
        account.access_token_encrypted = cipher.encrypt(
            new_access, user_id=account.user_id, platform=account.platform)
        if payload.get("refresh_token"):
            account.refresh_token_encrypted = cipher.encrypt(
                str(payload["refresh_token"]),
                user_id=account.user_id, platform=account.platform)
        if payload.get("expires_in"):
            account.token_expires_at = reference + timedelta(
                seconds=float(payload["expires_in"]))
        store.save(account)
        return new_access

    return cipher.decrypt(
        account.access_token_encrypted,
        user_id=account.user_id, platform=account.platform,
    )


def connection_report(store: AccountStore, user_id: str) -> list[dict]:
    """What Settings shows: every platform, and this user's state on it."""
    connected = {a.platform: a for a in store.list_for_user(user_id)}
    report = []
    for platform, provider in OAUTH_PROVIDERS.items():
        account = connected.get(platform)
        report.append({
            "platform": platform,
            "supported": provider.supported,
            "configured": provider.configured(),
            "connected": account is not None,
            "account": account.to_record() if account else None,
            "scopes": list(provider.scopes),
            "note": provider.note,
        })
    return report
