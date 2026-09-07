"""Account isolation, token encryption, and the no-passwords rule.

This is a multi-user product where one stored row is enough to post to a
stranger's social account. These tests exist to make the isolation rules hard
to remove by accident.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from viral.accounts import (
    OAUTH_PROVIDERS,
    AccountError,
    AccountIsolationError,
    AccountStore,
    ConnectFlow,
    TokenCipher,
    TokenSecurityError,
    build_authorization_url,
    connect_account,
    connection_report,
    generate_token_key,
    reject_password_credentials,
    resolve_access_token,
    verify_callback,
)

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
ALICE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
BOB = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


@pytest.fixture
def cipher():
    return TokenCipher(generate_token_key())


@pytest.fixture
def store():
    return AccountStore()


def _connect(store, cipher, user_id, platform="youtube", **over):
    params = dict(
        user_id=user_id, platform=platform,
        account_handle=f"@{user_id[:4]}", account_ref=f"ch_{user_id[:4]}",
        access_token=f"token-for-{user_id[:4]}", refresh_token=f"refresh-{user_id[:4]}",
        expires_in=3600, now=NOW, account_id=f"{platform}-{user_id[:4]}",
    )
    params.update(over)
    return connect_account(store, cipher, **params)


# --- no passwords, ever ----------------------------------------------------

@pytest.mark.parametrize("field_name", [
    "password", "Password", "account_password", "pwd", "passphrase",
    "two_factor_code", "otp", "security_answer", "pin",
])
def test_a_connect_payload_carrying_credentials_is_refused(field_name):
    with pytest.raises(AccountError, match="never asks for"):
        reject_password_credentials({"platform": "tiktok", field_name: "hunter2"})


def test_a_normal_oauth_payload_passes():
    reject_password_credentials(
        {"platform": "youtube", "code": "abc", "state": "xyz"})


def test_no_provider_offers_a_password_flow():
    for provider in OAUTH_PROVIDERS.values():
        assert provider.flow in {
            ConnectFlow.OAUTH_CODE, ConnectFlow.OAUTH_PKCE, ConnectFlow.UNSUPPORTED}


# --- token encryption ------------------------------------------------------

def test_tokens_round_trip(cipher):
    sealed = cipher.encrypt("secret-token", user_id=ALICE, platform="youtube")
    assert sealed != "secret-token"
    assert "secret-token" not in sealed
    assert cipher.decrypt(sealed, user_id=ALICE, platform="youtube") == "secret-token"


def test_a_ciphertext_stolen_into_another_users_row_will_not_decrypt(cipher):
    """The owner is authenticated data, so a copied row is inert."""
    sealed = cipher.encrypt("alice-token", user_id=ALICE, platform="youtube")
    with pytest.raises(TokenSecurityError):
        cipher.decrypt(sealed, user_id=BOB, platform="youtube")


def test_a_ciphertext_moved_to_another_platform_will_not_decrypt(cipher):
    sealed = cipher.encrypt("alice-token", user_id=ALICE, platform="youtube")
    with pytest.raises(TokenSecurityError):
        cipher.decrypt(sealed, user_id=ALICE, platform="tiktok")


def test_a_different_key_cannot_read_it(cipher):
    sealed = cipher.encrypt("alice-token", user_id=ALICE, platform="youtube")
    with pytest.raises(TokenSecurityError):
        TokenCipher(generate_token_key()).decrypt(
            sealed, user_id=ALICE, platform="youtube")


def test_a_tampered_ciphertext_is_rejected_not_silently_wrong(cipher):
    sealed = cipher.encrypt("alice-token", user_id=ALICE, platform="youtube")
    version, nonce, body = sealed.split(".", 2)
    flipped = body[:-4] + ("AAAA" if not body.endswith("AAAA") else "BBBB")
    with pytest.raises(TokenSecurityError):
        cipher.decrypt(f"{version}.{nonce}.{flipped}",
                       user_id=ALICE, platform="youtube")


def test_encryption_is_not_optional_when_no_key_is_configured(monkeypatch):
    """There is deliberately no plaintext fallback."""
    monkeypatch.delenv("VRF_TOKEN_KEY", raising=False)
    with pytest.raises(TokenSecurityError, match="never stored unencrypted"):
        TokenCipher()


def test_a_short_key_is_refused():
    with pytest.raises(TokenSecurityError, match="32 bytes"):
        TokenCipher("too-short")


def test_the_same_token_encrypts_differently_each_time(cipher):
    a = cipher.encrypt("t", user_id=ALICE, platform="youtube")
    b = cipher.encrypt("t", user_id=ALICE, platform="youtube")
    assert a != b, "a fixed nonce would leak which users share a token"


# --- OAuth flow binding ----------------------------------------------------

def test_the_authorization_url_carries_scopes_and_state(monkeypatch):
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "client-123")
    url, session = build_authorization_url(
        "youtube", ALICE, "https://app/callback", now=NOW)
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert "youtube.upload" in url
    assert session.state and session.state in url
    assert "access_type=offline" in url
    assert session.user_id == ALICE


def test_tiktok_uses_pkce(monkeypatch):
    monkeypatch.setenv("TIKTOK_CLIENT_KEY", "key-123")
    url, session = build_authorization_url(
        "tiktok", ALICE, "https://app/callback", now=NOW)
    assert "code_challenge_method=S256" in url
    assert session.code_verifier and session.code_verifier not in url


def test_snapchat_cannot_be_connected():
    with pytest.raises(AccountError, match="no server-side publishing API"):
        build_authorization_url("snapchat", ALICE, "https://app/callback")


def test_an_unconfigured_platform_says_which_variables_to_set(monkeypatch):
    monkeypatch.delenv("META_OAUTH_CLIENT_ID", raising=False)
    with pytest.raises(AccountError, match="META_OAUTH_CLIENT_ID"):
        build_authorization_url("instagram", ALICE, "https://app/callback")


def test_a_callback_finished_by_another_user_is_refused(monkeypatch):
    """Otherwise Bob's callback attaches Alice's account to Bob."""
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "client-123")
    _, session = build_authorization_url(
        "youtube", ALICE, "https://app/callback", now=NOW)
    with pytest.raises(AccountIsolationError):
        verify_callback(session, returned_state=session.state,
                        acting_user_id=BOB, now=NOW)


def test_a_mismatched_state_is_refused(monkeypatch):
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "client-123")
    _, session = build_authorization_url(
        "youtube", ALICE, "https://app/callback", now=NOW)
    with pytest.raises(AccountError, match="state did not match"):
        verify_callback(session, returned_state="forged",
                        acting_user_id=ALICE, now=NOW)


def test_an_expired_flow_is_refused(monkeypatch):
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "client-123")
    _, session = build_authorization_url(
        "youtube", ALICE, "https://app/callback", now=NOW)
    with pytest.raises(AccountError, match="expired"):
        verify_callback(session, returned_state=session.state,
                        acting_user_id=ALICE, now=NOW + timedelta(hours=1))


def test_a_valid_callback_passes(monkeypatch):
    monkeypatch.setenv("YOUTUBE_OAUTH_CLIENT_ID", "client-123")
    _, session = build_authorization_url(
        "youtube", ALICE, "https://app/callback", now=NOW)
    assert verify_callback(session, returned_state=session.state,
                           acting_user_id=ALICE, now=NOW) is session


# --- stored account isolation ----------------------------------------------

def test_a_user_only_lists_their_own_accounts(store, cipher):
    _connect(store, cipher, ALICE)
    _connect(store, cipher, BOB)
    _connect(store, cipher, BOB, platform="tiktok")

    assert [a.user_id for a in store.list_for_user(ALICE)] == [ALICE]
    assert len(store.list_for_user(BOB)) == 2


def test_fetching_by_platform_never_crosses_users(store, cipher):
    _connect(store, cipher, ALICE)
    assert store.get(BOB, "youtube") is None
    assert store.get(ALICE, "youtube").user_id == ALICE


def test_reading_another_users_account_by_id_raises(store, cipher):
    """Loudly, rather than returning None: this is an attempted breach."""
    alice_account = _connect(store, cipher, ALICE)
    with pytest.raises(AccountIsolationError):
        store.get_by_id(BOB, alice_account.id)


def test_revoking_another_users_account_is_refused(store, cipher):
    alice_account = _connect(store, cipher, ALICE)
    with pytest.raises(AccountIsolationError):
        store.revoke(BOB, alice_account.id, now=NOW)
    assert store.get_by_id(ALICE, alice_account.id).active is True


def test_deleting_another_users_account_is_refused(store, cipher):
    alice_account = _connect(store, cipher, ALICE)
    with pytest.raises(AccountIsolationError):
        store.delete(BOB, alice_account.id)
    assert store.get(ALICE, "youtube") is not None


def test_an_account_cannot_be_saved_without_an_owner(store, cipher):
    with pytest.raises(AccountIsolationError):
        _connect(store, cipher, "")


def test_two_users_on_the_same_platform_get_their_own_tokens(store, cipher):
    _connect(store, cipher, ALICE)
    _connect(store, cipher, BOB)
    assert resolve_access_token(
        store, cipher, user_id=ALICE, platform="youtube", now=NOW
    ) == "token-for-aaaa"
    assert resolve_access_token(
        store, cipher, user_id=BOB, platform="youtube", now=NOW
    ) == "token-for-bbbb"


def test_publishing_without_a_connected_account_is_refused(store, cipher):
    with pytest.raises(AccountError, match="No connected"):
        resolve_access_token(
            store, cipher, user_id=ALICE, platform="youtube", now=NOW)


# --- revocation and refresh ------------------------------------------------

def test_revoking_clears_the_stored_token_material(store, cipher):
    account = _connect(store, cipher, ALICE)
    store.revoke(ALICE, account.id, now=NOW)
    assert account.access_token_encrypted == ""
    assert account.refresh_token_encrypted == ""
    assert account.active is False


def test_a_revoked_account_cannot_publish(store, cipher):
    account = _connect(store, cipher, ALICE)
    store.revoke(ALICE, account.id, now=NOW)
    with pytest.raises(AccountError):
        resolve_access_token(
            store, cipher, user_id=ALICE, platform="youtube", now=NOW)


def test_a_token_near_expiry_is_refreshed_before_use(store, cipher):
    _connect(store, cipher, ALICE, expires_in=60)
    calls = []

    def refresher(platform, refresh_token):
        calls.append((platform, refresh_token))
        return {"access_token": "fresh-token", "expires_in": 3600}

    token = resolve_access_token(
        store, cipher, user_id=ALICE, platform="youtube",
        refresher=refresher, now=NOW)
    assert token == "fresh-token"
    assert calls == [("youtube", "refresh-aaaa")]
    # And the new token was stored encrypted, not in the clear.
    stored = store.get(ALICE, "youtube").access_token_encrypted
    assert "fresh-token" not in stored


def test_a_healthy_token_is_not_refreshed(store, cipher):
    _connect(store, cipher, ALICE, expires_in=7200)

    def refresher(platform, refresh_token):  # pragma: no cover - must not run
        raise AssertionError("refreshed a token that had not expired")

    assert resolve_access_token(
        store, cipher, user_id=ALICE, platform="youtube",
        refresher=refresher, now=NOW) == "token-for-aaaa"


def test_an_expired_token_with_no_refresher_asks_for_a_reconnect(store, cipher):
    _connect(store, cipher, ALICE, expires_in=10)
    with pytest.raises(AccountError, match="Reconnect"):
        resolve_access_token(
            store, cipher, user_id=ALICE, platform="youtube", now=NOW)


def test_a_failed_refresh_does_not_return_an_empty_token(store, cipher):
    _connect(store, cipher, ALICE, expires_in=10)
    with pytest.raises(AccountError, match="Reconnect"):
        resolve_access_token(
            store, cipher, user_id=ALICE, platform="youtube",
            refresher=lambda p, t: {"error": "invalid_grant"}, now=NOW)


# --- what leaves the server ------------------------------------------------

def test_the_ui_record_carries_no_token_material(store, cipher):
    account = _connect(store, cipher, ALICE)
    record = account.to_record()
    assert "access_token_encrypted" not in record
    assert "refresh_token_encrypted" not in record
    assert "user_id" not in record
    assert record["platform"] == "youtube"


def test_repr_does_not_leak_tokens(store, cipher):
    account = _connect(store, cipher, ALICE)
    assert "redacted" in repr(account)
    assert account.access_token_encrypted not in repr(account)


def test_the_settings_report_is_scoped_to_the_user(store, cipher):
    _connect(store, cipher, ALICE)
    _connect(store, cipher, BOB, platform="tiktok")

    alice = {r["platform"]: r for r in connection_report(store, ALICE)}
    assert alice["youtube"]["connected"] is True
    assert alice["tiktok"]["connected"] is False
    assert alice["snapchat"]["supported"] is False
    assert alice["snapchat"]["note"]
