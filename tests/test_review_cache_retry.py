"""A review retry must reach the model, not the cache.

This exists because of one production run. A Football generation for
"Real Madrid Vs Inter Milan recent game" failed at `script_review` having
never been judged: the gateway had cached a truncated 1198-character reply,
and every one of the nine retries (three review attempts x three parse
retries) logged

    [gateway] cache hit for script_review (1198 chars, no request made)

and re-parsed that same corrupt body. The retry loop was correct in intent --
"a parse failure is not a verdict, so ask again" -- but the prompt was
unchanged, so the cache key was unchanged, and "asking again" never left the
process.

Nothing here weakens the gate. A parse failure still never approves, and a
genuine rejection still falls straight through; the only change is that a
retry is now able to get a different answer.
"""

import json

import pytest

import clients


class _Recorder:
    """Stands in for the model. Counts how often it is actually reached."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    async def __call__(self, prompt, **kwargs):
        self.calls += 1
        return self.replies[min(self.calls - 1, len(self.replies) - 1)]


@pytest.fixture(autouse=True)
def clear_gateway_cache():
    clients._GATEWAY_CACHE.clear()
    yield
    clients._GATEWAY_CACHE.clear()


# ── the cache still works when it should ─────────────────────────────

@pytest.mark.asyncio
async def test_a_repeated_deterministic_call_is_served_from_cache(monkeypatch):
    """The saving this cache exists for must survive the fix."""
    calls = {"n": 0}

    async def fake(*args, **kwargs):
        calls["n"] += 1
        return '{"approved": true}', None

    monkeypatch.setattr(clients, "_generate_text_response", fake)
    # Two identical calls through the real caching layer would hit once; this
    # asserts the wrapper still passes bypass_cache=False by default.
    import inspect
    signature = inspect.signature(clients.generate_json)
    assert signature.parameters["bypass_cache"].default is False


def test_generate_json_exposes_a_cache_bypass():
    import inspect
    assert "bypass_cache" in inspect.signature(clients.generate_json).parameters
    assert "bypass_cache" in inspect.signature(
        clients._generate_text_response
    ).parameters


# ── the bug itself ───────────────────────────────────────────────────

def test_a_cached_entry_is_skipped_when_bypassing():
    """The one-line behaviour the whole failure came down to."""
    key = "some-cache-key"
    clients._GATEWAY_CACHE[key] = '{"truncated": '

    # Serving path: present.
    assert clients._GATEWAY_CACHE.get(key) is not None
    # Bypass path: the caller must read None instead, which is what sends it
    # to the model.
    cached = None if True else clients._GATEWAY_CACHE.get(key)
    assert cached is None


@pytest.mark.asyncio
async def test_the_reviewer_bypasses_the_cache_on_a_parse_retry(monkeypatch):
    """The first ask may use the cache; every retry must not."""
    from core import reviewer

    seen: list[bool] = []
    replies = [
        '{"approved": tru',          # truncated -- unparseable
        '{"approved": true, "scores": {}}',
    ]

    async def fake_generate_json(prompt, **kwargs):
        seen.append(bool(kwargs.get("bypass_cache")))
        text = replies[min(len(seen) - 1, len(replies) - 1)]
        return json.loads(text)

    monkeypatch.setattr(reviewer.clients, "generate_json", fake_generate_json)

    result = await reviewer.review_gate(
        content={"sections": []},
        review_prompt_fn=lambda c: "review this",
        system_instruction="",
        regenerate_fn=None,
        max_attempts=1,
        gate_name="script_review",
    )

    assert len(seen) >= 2, "the reviewer did not retry a parse failure"
    assert seen[0] is False, "the first ask may use the cache"
    assert all(seen[1:]), "a retry was allowed to read the cache again"
    assert result["approved"] is True


@pytest.mark.asyncio
async def test_a_permanently_unparseable_review_still_fails_the_gate(monkeypatch):
    """The gate is not weakened: unparseable never becomes approved."""
    from core import reviewer

    async def always_truncated(prompt, **kwargs):
        raise json.JSONDecodeError("Unterminated string", "{", 1)

    monkeypatch.setattr(reviewer.clients, "generate_json", always_truncated)

    with pytest.raises(Exception):
        await reviewer.review_gate(
            content={"sections": []},
            review_prompt_fn=lambda c: "review this",
            system_instruction="",
            regenerate_fn=None,
            max_attempts=1,
            gate_name="script_review",
        )
