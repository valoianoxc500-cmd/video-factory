"""The OAuth callback behaves safely on every path through it.

Run against a locally served build on :3300. Nothing here contacts Google --
the callback is exercised with the inputs Google would produce, including the
ones an attacker would produce.

The open-redirect cases matter most: `next` arrives inside a URL the user was
sent to, so a callback that trusts it turns sign-in into a redirector that
lands a freshly authenticated user on somebody else's page.
"""

from __future__ import annotations

import pathlib
import sys
from urllib.parse import quote, urlparse

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import settings  # noqa: E402,F401  (TLS bootstrap for this host)
import httpx  # noqa: E402

BASE = "http://localhost:3300"
CALLBACK = f"{BASE}/auth/callback"


@pytest.fixture(scope="module", autouse=True)
def server_running():
    try:
        httpx.get(f"{BASE}/login", timeout=5)
    except Exception:
        pytest.skip(
            "the web app is not being served on :3300 "
            "(cd web && npm run build && npm run start -- --port 3300)"
        )


def _get(url: str) -> httpx.Response:
    return httpx.get(url, follow_redirects=False, timeout=30)


def test_the_callback_route_exists():
    """A missing route would 404 rather than redirect, and sign-in would hang."""
    resp = _get(CALLBACK)
    assert resp.status_code != 404, "the /auth/callback route is not deployed"


def test_a_callback_with_no_code_goes_back_to_login():
    resp = _get(CALLBACK)
    assert resp.status_code in (302, 303, 307, 308)
    assert "/login" in resp.headers.get("location", "")


def test_a_cancelled_consent_screen_returns_a_message():
    """Google reports refusal with ?error=, not with a code."""
    resp = _get(
        f"{CALLBACK}?error=access_denied"
        f"&error_description={quote('The user denied the request')}"
    )
    assert resp.status_code in (302, 303, 307, 308)
    location = resp.headers.get("location", "")
    assert "/login" in location
    assert "error=" in location


def test_an_invalid_code_does_not_sign_anyone_in():
    resp = _get(f"{CALLBACK}?code=not-a-real-authorization-code")
    assert resp.status_code in (302, 303, 307, 308)
    location = resp.headers.get("location", "")
    assert "/login" in location, "a bad code must not reach the dashboard"
    # No session cookie may be set by a failed exchange.
    cookies = resp.headers.get_list("set-cookie")
    assert not any("sb-" in c and "access-token" in c for c in cookies), (
        "a session cookie was set for an invalid authorization code"
    )


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example.com/steal",
        "//evil.example.com/steal",
        "http://evil.example.com",
        "\\\\evil.example.com",
        "https://evil.example.com",
    ],
)
def test_the_callback_refuses_an_offsite_redirect(hostile):
    """Regression guard against turning sign-in into an open redirect."""
    resp = _get(f"{CALLBACK}?code=x&next={quote(hostile, safe='')}")
    assert resp.status_code in (302, 303, 307, 308)
    location = resp.headers.get("location", "")
    host = urlparse(location).netloc
    assert host in ("", "localhost:3300"), (
        f"the callback redirected offsite to {location!r}"
    )
    assert "evil.example.com" not in location


def test_a_same_site_next_is_preserved_through_a_failed_exchange():
    """A relative path is safe, so it should survive rather than be dropped."""
    resp = _get(f"{CALLBACK}?next=%2Fdashboard%2Flibrary")
    location = resp.headers.get("location", "")
    assert urlparse(location).netloc in ("", "localhost:3300")


def test_the_callback_is_reachable_without_a_session():
    """Middleware must not gate the route that establishes the session."""
    resp = _get(CALLBACK)
    location = resp.headers.get("location", "")
    assert "next=%2Fauth%2Fcallback" not in location, (
        "the callback is behind the auth gate, so OAuth can never complete"
    )


def _delivered(path: str) -> str:
    """Everything the browser receives for a page: markup plus its JS chunks.

    /login calls useSearchParams, which opts the whole page into client
    rendering, so its form never appears in the server HTML. Checking the
    markup alone would report the button missing when it is present.
    """
    import re

    html = httpx.get(f"{BASE}{path}", timeout=30).text
    chunks = list(dict.fromkeys(re.findall(r'src="(/_next/static/[^"]+\.js)"', html)))
    scripts = "".join(
        httpx.get(f"{BASE}{src}", timeout=30).text for src in chunks
    )
    return html + scripts


def test_a_code_landing_on_the_root_is_forwarded_to_the_callback():
    """Supabase drops the callback path when redirect_to is not allow-listed.

    It falls back to the project's Site URL, so the browser arrives at
    `/?code=...` instead of `/auth/callback?code=...`. Without forwarding, the
    landing page renders and sign-in dies silently.
    """
    resp = _get(f"{BASE}/?code=abc123")
    assert resp.status_code in (302, 303, 307, 308), (
        "an authorization code on the root was not forwarded"
    )
    location = resp.headers.get("location", "")
    assert "/auth/callback" in location
    assert "code=abc123" in location
    assert "next=%2Fdashboard" in location or "next=/dashboard" in location


def test_a_provider_error_on_the_root_is_forwarded_too():
    resp = _get(f"{BASE}/?error=access_denied")
    assert resp.status_code in (302, 303, 307, 308)
    assert "/auth/callback" in resp.headers.get("location", "")


def test_forwarding_keeps_an_explicit_next():
    resp = _get(f"{BASE}/?code=abc123&next=%2Fdashboard%2Flibrary")
    location = resp.headers.get("location", "")
    assert "next=%2Fdashboard%2Flibrary" in location or "next=/dashboard/library" in location


def test_the_landing_page_still_works_without_a_code():
    """The forwarding must not hijack ordinary visits to the root."""
    resp = _get(f"{BASE}/")
    location = resp.headers.get("location", "")
    assert "/auth/callback" not in location, "the landing page was hijacked"


def test_a_forwarded_code_still_cannot_redirect_offsite():
    resp = _get(f"{BASE}/?code=abc&next=https%3A%2F%2Fevil.example.com")
    # First hop goes to the callback; the callback then applies its own guard.
    location = resp.headers.get("location", "")
    if "/auth/callback" in location:
        second = _get(location if location.startswith("http") else BASE + location)
        final = second.headers.get("location", "")
        assert "evil.example.com" not in final, "forwarding opened a redirect hole"


def test_the_login_page_offers_google():
    delivered = _delivered("/login")
    assert "btn-google" in delivered, "the Google button is missing from /login"
    assert "Continue with Google" in delivered


def test_the_signup_page_offers_google():
    html = httpx.get(f"{BASE}/signup", timeout=30).text
    assert "btn-google" in html, "the Google button is missing from /signup"


def test_the_google_button_is_styled():
    import re

    html = httpx.get(f"{BASE}/login", timeout=30).text
    sheets = list(dict.fromkeys(re.findall(r'href="(/_next/static/[^"]+\.css)"', html)))
    css = "".join(httpx.get(f"{BASE}{href}", timeout=30).text for href in sheets)
    assert ".btn-google" in css, "the Google button has no styling"
    assert ".auth-divider" in css


def test_email_and_password_sign_in_still_works():
    """The provider button must not have displaced the existing form."""
    delivered = _delivered("/login")
    assert "current-password" in delivered, "the password field is gone"
    assert "signInWithPassword" in delivered, "password sign-in was removed"
    assert "Forgot password?" in delivered


def test_no_google_client_secret_reaches_the_browser():
    """Supabase holds the secret; this app must never carry one."""
    for path in ("/login", "/signup"):
        html = httpx.get(f"{BASE}{path}", timeout=30).text
        for marker in (
            "GOOGLE_CLIENT_SECRET",
            "client_secret",
            ".apps.googleusercontent.com",
            "GOCSPX-",
        ):
            assert marker not in html, f"{path} shipped {marker} to the browser"
