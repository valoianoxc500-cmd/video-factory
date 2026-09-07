"""Every protected surface refuses an unauthenticated caller.

Run against a locally served build (`npm run build && npm run start` in web/,
port 3300). A middleware gate is easy to write and easy to get subtly wrong --
a matcher that misses a path leaves a page or an endpoint open -- so each one
is requested directly rather than assumed covered.

Skips cleanly when the server is not running, so the suite stays runnable
without it.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
import settings  # noqa: E402,F401  (TLS bootstrap for this host)
import httpx  # noqa: E402

BASE = "http://localhost:3300"

PROTECTED_PAGES = [
    "/dashboard",
    "/dashboard/create",
    "/dashboard/library",
    "/dashboard/jobs",
    "/dashboard/settings",
    "/dashboard/channels/horror_stories",
    "/dashboard/channels/football_news",
]

PROTECTED_APIS = [
    ("GET", "/api/videos"),
    ("GET", "/api/jobs"),
    ("POST", "/api/jobs"),
    ("GET", "/api/library"),
    ("GET", "/api/jobs/00000000-0000-0000-0000-000000000000"),
    ("GET", "/api/media/00000000-0000-0000-0000-000000000000/video"),
    ("GET", "/api/media/00000000-0000-0000-0000-000000000000/thumbnail"),
]

PUBLIC_PAGES = ["/login", "/signup", "/forgot-password"]

# Google's OAuth reviewers fetch these with no session; a middleware change that
# swept them behind the gate would fail verification silently.
LEGAL_PAGES = ["/privacy-policy", "/terms"]


@pytest.fixture(scope="module", autouse=True)
def server_running():
    try:
        httpx.get(f"{BASE}/login", timeout=5)
    except Exception:
        pytest.skip(
            "the web app is not being served on :3300 "
            "(cd web && npm run build && npm run start -- --port 3300)"
        )


@pytest.mark.parametrize("path", PROTECTED_PAGES)
def test_a_signed_out_visitor_is_sent_to_login(path):
    resp = httpx.get(f"{BASE}{path}", follow_redirects=False, timeout=30)
    assert resp.status_code in (302, 303, 307, 308), (
        f"{path} served content to a signed-out visitor ({resp.status_code})"
    )
    assert "/login" in resp.headers.get("location", ""), (
        f"{path} redirected somewhere other than login"
    )


@pytest.mark.parametrize("path", PROTECTED_PAGES)
def test_no_protected_page_leaks_its_content(path):
    """A redirect that still ships the page body would defeat the gate.

    Checked against dashboard markup, not against words like "library" --
    those legitimately appear in the ?next= parameter of the redirect itself.
    """
    resp = httpx.get(f"{BASE}{path}", follow_redirects=False, timeout=30)
    body = resp.text.lower()
    for marker in ("sign out", "<nav", "vcard", "dashboard-shell", "<video"):
        assert marker not in body, (
            f"{path} shipped dashboard markup ({marker}) to a signed-out visitor"
        )
    assert len(resp.text) < 1024, (
        f"{path} returned a {len(resp.text)}-byte body with its redirect"
    )


@pytest.mark.parametrize("method,path", PROTECTED_APIS)
def test_an_api_refuses_an_unauthenticated_caller(method, path):
    resp = httpx.request(
        method,
        f"{BASE}{path}",
        follow_redirects=False,
        timeout=30,
        json={"topic": "unauthenticated", "engine": "horror_stories"}
        if method == "POST"
        else None,
    )
    assert resp.status_code in (302, 303, 307, 308, 401, 403, 404), (
        f"{method} {path} answered an unauthenticated caller with "
        f"{resp.status_code}"
    )
    if resp.status_code == 200:
        pytest.fail(f"{method} {path} returned data without a session")


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_the_auth_pages_are_reachable(path):
    """The gate must not lock out the pages needed to get through it."""
    resp = httpx.get(f"{BASE}{path}", follow_redirects=False, timeout=30)
    assert resp.status_code == 200, f"{path} is not reachable ({resp.status_code})"


@pytest.mark.parametrize("path", LEGAL_PAGES)
def test_a_legal_page_is_public(path):
    """No session, no redirect: 200 and readable."""
    resp = httpx.get(f"{BASE}{path}", follow_redirects=False, timeout=30)
    assert resp.status_code == 200, (
        f"{path} is not publicly reachable ({resp.status_code}) -- "
        "Google OAuth verification fetches it without signing in"
    )


@pytest.mark.parametrize("path", LEGAL_PAGES)
def test_a_legal_page_has_real_content(path):
    """An empty shell would pass a status check and fail a human review."""
    html = httpx.get(f"{BASE}{path}", timeout=30).text
    assert len(html) > 4000, f"{path} looks too short to be a real document"
    assert "Video Factory" in html, f"{path} does not name the service"
    assert "YOUR APP NAME" not in html, (
        f"{path} still shows the branding placeholder"
    )
    for phrase in ("Google", "data"):
        assert phrase.lower() in html.lower(), f"{path} never mentions {phrase}"


def test_the_privacy_policy_covers_what_the_service_does():
    html = httpx.get(f"{BASE}/privacy-policy", timeout=30).text.lower()
    for topic in ("google", "email", "cookie", "library", "delete", "retention"):
        assert topic in html, f"the privacy policy does not cover {topic}"


def test_the_terms_cover_generated_content():
    html = httpx.get(f"{BASE}/terms", timeout=30).text.lower()
    for topic in ("account", "generated", "liability", "acceptable use"):
        assert topic in html, f"the terms do not cover {topic}"


def test_the_legal_pages_make_no_certification_claims():
    """Unsupported compliance claims are a liability, not a selling point."""
    forbidden = [
        "soc 2",
        "soc2",
        "iso 27001",
        "hipaa compliant",
        "pci dss",
        "gdpr compliant",
        "gdpr certified",
        "fully compliant",
        "certified secure",
    ]
    for path in LEGAL_PAGES:
        html = httpx.get(f"{BASE}{path}", timeout=30).text.lower()
        for claim in forbidden:
            assert claim not in html, f"{path} claims {claim!r}"


@pytest.mark.parametrize("path", PUBLIC_PAGES)
def test_the_auth_pages_link_to_the_legal_pages(path):
    delivered = httpx.get(f"{BASE}{path}", timeout=30).text
    assert "/privacy-policy" in delivered, f"{path} does not link the privacy policy"
    assert "/terms" in delivered, f"{path} does not link the terms"


@pytest.mark.parametrize("path", LEGAL_PAGES)
def test_a_legal_page_is_styled(path):
    import re

    html = httpx.get(f"{BASE}{path}", timeout=30).text
    sheets = list(dict.fromkeys(re.findall(r'href="(/_next/static/[^"]+\.css)"', html)))
    assert sheets, f"{path} links no stylesheet"
    css = "".join(httpx.get(f"{BASE}{href}", timeout=30).text for href in sheets)
    assert ".legal-body" in css, f"{path} would render unstyled"


def test_the_worker_endpoints_reject_a_missing_token():
    for path in ("/api/worker/claim", "/api/worker/update"):
        resp = httpx.post(f"{BASE}{path}", json={}, timeout=30)
        assert resp.status_code in (401, 403), (
            f"{path} accepted a request with no worker token ({resp.status_code})"
        )


def test_the_worker_endpoints_reject_a_wrong_token():
    for path in ("/api/worker/claim", "/api/worker/update"):
        resp = httpx.post(
            f"{BASE}{path}",
            json={},
            headers={"authorization": "Bearer not-the-worker-token"},
            timeout=30,
        )
        assert resp.status_code in (401, 403), (
            f"{path} accepted a wrong worker token ({resp.status_code})"
        )


def test_the_landing_page_does_not_expose_the_dashboard():
    resp = httpx.get(f"{BASE}/", follow_redirects=False, timeout=30)
    assert resp.status_code in (200, 302, 303, 307, 308)
    if resp.status_code == 200:
        assert "sign out" not in resp.text.lower()


@pytest.mark.parametrize("path", PUBLIC_PAGES + ["/reset-password"])
def test_an_auth_page_ships_the_design_system(path):
    """Regression: the stylesheet was imported only by the dashboard layout.

    Every page outside /dashboard -- login, signup and both password pages --
    rendered as unstyled HTML, which a build and a typecheck both pass.
    """
    import re

    html = httpx.get(f"{BASE}{path}", timeout=30).text
    sheets = list(dict.fromkeys(re.findall(r'href="(/_next/static/[^"]+\.css)"', html)))
    assert sheets, f"{path} links no stylesheet at all"

    css = "".join(httpx.get(f"{BASE}{href}", timeout=30).text for href in sheets)
    assert "auth-shell" in html, f"{path} does not use the auth layout"
    for rule in (".auth-shell", ".auth-card", ".field input", ".btn-primary"):
        assert rule in css, f"{path} is missing the {rule} rule -- it will render bare"


def test_no_page_ships_a_service_credential():
    """A secret pasted into a client component would ship to the browser."""
    for path in PUBLIC_PAGES + ["/"]:
        body = httpx.get(f"{BASE}{path}", timeout=30).text
        for marker in (
            "service_role",
            "-----BEGIN PRIVATE KEY-----",
            "GCS_SERVICE_ACCOUNT_JSON",
            "SUPABASE_SERVICE",
        ):
            assert marker not in body, f"{path} shipped {marker} to the browser"
