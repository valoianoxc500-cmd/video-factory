"""The web app and the worker must agree.

Three tables are written twice â€” once in TypeScript for the browser, once in
Python for the worker â€” because neither runtime can call the other. Drift
between them is a nasty class of bug: a scope missing on one side produces a
token that authenticates but cannot publish; a platform the UI offers and the
backend refuses produces a job that fails hours later. These tests read the
TypeScript and compare it to the Python, so drift fails here instead.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from viral.accounts import OAUTH_PROVIDERS, ConnectFlow
from viral.publishing import CAPABILITIES, CAPTION_LIMITS, Capability

WEB = Path(__file__).resolve().parent.parent / "web" / "lib"
OAUTH_TS = WEB / "vrf-oauth.ts"
VRF_TS = WEB / "vrf.ts"
PUBLISH_TS = WEB / "vrf-publish.ts"


def _read(path: Path) -> str:
    if not path.exists():
        pytest.skip(f"{path.name} is not in this checkout")
    return path.read_text(encoding="utf-8")


#: A TS value: a string (possibly written as several concatenated literals),
#: a boolean, or an array of strings.
_VALUE = r'("(?:[^"\\]|\\.)*"(?:\s*\+\s*"(?:[^"\\]|\\.)*")*|true|false|\[[^\]]*\])'
_PAIR = re.compile(rf"(\w+):\s*{_VALUE}")


def _ts_entries(source: str, start_marker: str) -> list[dict]:
    """The `key: value` pairs of each entry in a TS array/record literal.

    Deliberately small: it handles the two literals this file checks, splitting
    on the `platform:` key that starts each entry, rather than pretending to
    parse TypeScript.
    """
    # Start at the "=", so a type annotation like `Foo[]` is not mistaken for
    # the start of the literal.
    start = source.index("=", source.index(start_marker))
    depth = 0
    end = start
    opened = False
    for index in range(start, len(source)):
        char = source[index]
        if char in "{[":
            depth += 1
            opened = True
        elif char in "}]":
            depth -= 1
            if opened and depth == 0:
                end = index + 1
                break
    body = source[start:end]

    starts = [m.start() for m in re.finditer(r"\bplatform:\s*\"", body)]
    entries: list[dict] = []
    for position, begin in enumerate(starts):
        finish = starts[position + 1] if position + 1 < len(starts) else len(body)
        entries.append({k: v for k, v in _PAIR.findall(body[begin:finish])})
    return entries


def _ts_string(raw: str) -> str:
    """Join a TS string written as concatenated literals."""
    return "".join(json.loads(part) for part in re.findall(r'"(?:[^"\\]|\\.)*"', raw))


def _string_list(raw: str) -> list[str]:
    return re.findall(r'"((?:[^"\\]|\\.)*)"', raw or "")


# --- OAuth providers -------------------------------------------------------

@pytest.fixture(scope="module")
def ts_providers() -> dict[str, dict]:
    blocks = _ts_entries(_read(OAUTH_TS), "export const OAUTH_PROVIDERS")
    return {
        json.loads(b["platform"]): b for b in blocks if "platform" in b and "flow" in b
    }


def test_the_same_platforms_exist_on_both_sides(ts_providers):
    assert set(ts_providers) == set(OAUTH_PROVIDERS)


@pytest.mark.parametrize("platform", sorted(OAUTH_PROVIDERS))
def test_scopes_match(platform, ts_providers):
    """A scope on one side only means a token that cannot do the job."""
    expected = list(OAUTH_PROVIDERS[platform].scopes)
    actual = _string_list(ts_providers[platform].get("scopes", "[]"))
    assert actual == expected, f"{platform} scopes drifted"


@pytest.mark.parametrize("platform", sorted(OAUTH_PROVIDERS))
def test_flows_match(platform, ts_providers):
    expected = OAUTH_PROVIDERS[platform].flow.value
    assert json.loads(ts_providers[platform]["flow"]) == expected


@pytest.mark.parametrize("platform", sorted(OAUTH_PROVIDERS))
def test_endpoints_match(platform, ts_providers):
    provider = OAUTH_PROVIDERS[platform]
    block = ts_providers[platform]
    assert json.loads(block.get("authorizeUrl", '""')) == provider.authorize_url
    assert json.loads(block.get("tokenUrl", '""')) == provider.token_url


@pytest.mark.parametrize("platform", sorted(OAUTH_PROVIDERS))
def test_credential_environment_variables_match(platform, ts_providers):
    provider = OAUTH_PROVIDERS[platform]
    block = ts_providers[platform]
    assert json.loads(block.get("clientIdEnv", '""')) == provider.client_id_env
    assert json.loads(block.get("clientSecretEnv", '""')) == provider.client_secret_env


def test_snapchat_is_unsupported_on_both_sides(ts_providers):
    assert OAUTH_PROVIDERS["snapchat"].flow is ConnectFlow.UNSUPPORTED
    assert json.loads(ts_providers["snapchat"]["flow"]) == "unsupported"


# --- publishing capabilities ----------------------------------------------

@pytest.fixture(scope="module")
def ts_platforms() -> dict[str, dict]:
    blocks = _ts_entries(_read(VRF_TS), "export const PLATFORMS")
    return {
        json.loads(b["platform"]): b for b in blocks if "platform" in b and "label" in b
    }


def test_the_same_publishable_platforms_exist_on_both_sides(ts_platforms):
    assert set(ts_platforms) == set(CAPABILITIES)


@pytest.mark.parametrize("platform", sorted(CAPABILITIES))
def test_publish_capability_matches(platform, ts_platforms):
    """A UI that offers what the backend refuses is worse than not offering it."""
    expected = CAPABILITIES[platform].can_publish
    assert ts_platforms[platform]["canPublish"] == str(expected).lower()


@pytest.mark.parametrize("platform", sorted(CAPABILITIES))
def test_native_scheduling_matches(platform, ts_platforms):
    expected = Capability.NATIVE_SCHEDULING in CAPABILITIES[platform].capabilities
    assert ts_platforms[platform]["nativeScheduling"] == str(expected).lower()


@pytest.mark.parametrize("platform", sorted(CAPABILITIES))
def test_supported_matches(platform, ts_platforms):
    expected = Capability.UNSUPPORTED not in CAPABILITIES[platform].capabilities
    assert ts_platforms[platform]["supported"] == str(expected).lower()


def test_snapchat_is_refused_on_both_sides(ts_platforms):
    assert CAPABILITIES["snapchat"].can_publish is False
    assert ts_platforms["snapchat"]["canPublish"] == "false"
    note = _ts_string(ts_platforms["snapchat"]["note"])
    assert "no server-side publishing API" in note


# --- caption limits --------------------------------------------------------

def test_caption_limits_match():
    source = _read(PUBLISH_TS)
    block = source[source.index("CAPTION_LIMITS"):]
    block = block[: block.index("}")]
    actual = {
        platform: int(limit)
        for platform, limit in re.findall(r"(\w+):\s*(\d+)", block)
    }
    assert actual == CAPTION_LIMITS


# --- rights bases ----------------------------------------------------------

def test_discovered_is_not_offered_as_a_rights_basis():
    """The one option that must never appear in the form."""
    source = _read(VRF_TS)
    block = source[source.index("export const RIGHTS_SOURCES"):]
    block = block[: block.index("] as const")]
    values = re.findall(r'value:\s*"([^"]+)"', block)

    assert "discovered" not in values
    assert set(values) == {
        # The single confirmation the Add-a-video form collects. Recorded as
        # its own value rather than mapped onto own_recording, so the stored
        # attestation says exactly what the user was asked.
        "owned_or_permitted",
        "own_recording", "licensed", "permission",
        "creative_commons", "public_domain",
    }


def test_every_offered_rights_basis_is_publishable_in_python():
    from viral.rights import PUBLISHABLE_SOURCES, Source

    source = _read(VRF_TS)
    block = source[source.index("export const RIGHTS_SOURCES"):]
    block = block[: block.index("] as const")]
    for value in re.findall(r'value:\s*"([^"]+)"', block):
        assert Source(value) in PUBLISHABLE_SOURCES, (
            f"the form offers {value!r}, which the rights gate refuses"
        )

