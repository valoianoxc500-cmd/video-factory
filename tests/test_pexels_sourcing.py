"""Pexels query planning, ranking and cross-beat deduplication.

The sourcer used to send one query per beat and rank what came back by shape
alone. That meant an unlucky phrasing cost the beat its picture, near-copies
from one shoot filled the candidate set, and two beats running concurrently
could ship the same photograph. These cover the replacement.
"""

import asyncio
import io

import httpx
import pytest
from PIL import Image

import core.image_sourcer as image_sourcer


@pytest.fixture(autouse=True)
def _clean_pexels_dedup(monkeypatch):
    """The claim/commit sets are per run, so no test may inherit another's."""
    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")
    image_sourcer._reset_pexels_dedup()
    yield
    image_sourcer._reset_pexels_dedup()


class _FakeResponse:
    def __init__(self, json_data=None, content=b""):
        self._json_data = json_data
        self.content = content
        self.status_code = 200
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


def _jpg(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 9), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _photo(
    photo_id: int,
    *,
    alt: str = "",
    photographer: str = "p",
    width: int = 1080,
    height: int = 1920,
) -> dict:
    return {
        "id": photo_id,
        "alt": alt,
        "width": width,
        "height": height,
        "photographer": photographer,
        "photographer_id": photographer,
        "url": f"https://www.pexels.com/photo/example-{photo_id}/",
        "src": {"large2x": f"https://img.test/{photo_id}.jpg"},
    }


class _LadderClient:
    """Answers the whole query grid, and can fail chosen rungs."""

    def __init__(self, photos_by_query: dict, *, failing_queries=()):
        self.photos_by_query = photos_by_query
        self.failing_queries = set(failing_queries)
        self.searches: list[dict] = []
        self.downloads: list[str] = []

    async def get(self, url: str, **kwargs):
        if url == "https://api.pexels.com/v1/search":
            params = kwargs.get("params") or {}
            self.searches.append(params)
            if params.get("query") in self.failing_queries:
                raise httpx.ConnectError("pexels unavailable")
            photos = self.photos_by_query.get(params.get("query"), [])
            return _FakeResponse(json_data={"photos": list(photos)})

        self.downloads.append(url)
        # Colour keyed off the photo id so every photo is distinct bytes.
        photo_id = int(url.rsplit("/", 1)[-1].split(".")[0])
        return _FakeResponse(content=_jpg((photo_id % 256, 40, 80)))


def _collect(client, keywords: str, tmp_path, *, prompt: str = "", limit: int = 8):
    return asyncio.run(
        image_sourcer._collect_pexels_candidates(
            keywords=keywords,
            prompt=prompt,
            output_name="section_001_01.jpg",
            client=client,
            seen_hashes=set(),
            target_size=(16, 9),
            tmp_dir=tmp_path,
            limit=limit,
        )
    )


# --- query ladder ---------------------------------------------------------


def test_ladder_leads_with_the_briefs_own_words():
    ladder = image_sourcer.pexels_query_ladder(
        "rusted iron gate at an abandoned asylum"
    )

    assert ladder[0] == "rusted iron gate at an abandoned asylum"
    assert len(ladder) > 1, "a beat should have somewhere to fall back to"


def test_ladder_drops_mood_words_that_no_caption_carries():
    ladder = image_sourcer.pexels_query_ladder(
        "eerie haunting abandoned lighthouse"
    )

    assert "eerie" not in " ".join(ladder)
    assert "haunting" not in " ".join(ladder)
    assert any("lighthouse" in query for query in ladder)


def test_ladder_strips_the_staging_around_a_subject():
    ladder = image_sourcer.pexels_query_ladder(
        "hands holding stacks of 1971 twenty dollar bills"
    )

    assert any(
        "holding" not in query and "1971" in query and "dollar" in query
        for query in ladder
    ), ladder


def test_ladder_ends_on_the_trailing_noun_phrase():
    ladder = image_sourcer.pexels_query_ladder(
        "a lone red parachute descending over dense pine forest"
    )

    assert ladder[-1].split() == ["pine", "forest"]


def test_ladder_is_deduplicated_and_capped():
    ladder = image_sourcer.pexels_query_ladder("snow tent", limit=3)

    assert len(ladder) <= 3
    assert len(ladder) == len({query.lower() for query in ladder})


def test_ladder_prefers_latin_queries_but_keeps_arabic_when_alone():
    mixed = image_sourcer.pexels_query_ladder("ملعب كرة القدم stadium floodlights")
    assert all(
        image_sourcer._latin_ratio(query)
        >= image_sourcer._MIN_LATIN_RATIO_FOR_SEARCH
        for query in mixed
    )

    arabic_only = image_sourcer.pexels_query_ladder("ملعب كرة القدم")
    assert arabic_only, "an Arabic-only brief must still be searchable"


def test_ladder_falls_back_to_the_prompt_when_keywords_are_thin():
    ladder = image_sourcer.pexels_query_ladder(
        "the", prompt="a wooden fishing boat pulled onto a grey beach"
    )

    assert any("boat" in query for query in ladder), ladder


def test_ladder_is_empty_only_when_there_is_nothing_to_search_for():
    assert image_sourcer.pexels_query_ladder("") == []
    assert image_sourcer.pexels_query_ladder("   ") == []


# --- relevance scoring ----------------------------------------------------


def test_relevance_prefers_the_photo_whose_caption_matches_the_beat():
    tokens = ["torn", "tent", "snow"]
    matching = _photo(1, alt="a torn tent half buried in snow")
    generic = _photo(2, alt="a woman smiling in an office")

    assert image_sourcer._pexels_relevance(matching, tokens) > (
        image_sourcer._pexels_relevance(generic, tokens)
    )


def test_relevance_rewards_words_that_sit_together():
    tokens = ["fishing", "boat"]
    together = _photo(1, alt="a wooden fishing boat at dawn")
    scattered = _photo(2, alt="a boat, and separately some fishing gear")

    assert image_sourcer._pexels_relevance(together, tokens) > (
        image_sourcer._pexels_relevance(scattered, tokens)
    )


def test_relevance_beats_shape_when_ranking_candidates():
    tokens = ["stadium", "floodlights"]
    # Relevant but the wrong shape, against irrelevant but perfectly vertical.
    relevant_landscape = _photo(
        1, alt="stadium floodlights at night", width=1920, height=1080
    )
    irrelevant_portrait = _photo(2, alt="a bowl of soup", width=1080, height=1920)

    target = (1080, 1920)
    assert image_sourcer._pexels_rank(relevant_landscape, tokens, target) > (
        image_sourcer._pexels_rank(irrelevant_portrait, tokens, target)
    )


def test_ranking_puts_the_relevant_result_first(tmp_path):
    photos = [
        _photo(1, alt="an empty meeting room"),
        _photo(2, alt="a torn tent half buried in snow"),
    ]
    client = _LadderClient({"torn tent snow": photos})

    records = _collect(client, "torn tent snow", tmp_path, limit=2)

    assert [record["pexels_id"] for record in records] == ["2", "1"]


# --- result diversification ----------------------------------------------


def test_one_photographer_cannot_fill_the_candidate_set():
    photos = [_photo(i, photographer="same") for i in range(1, 7)]
    photos += [_photo(7, photographer="b"), _photo(8, photographer="c")]

    picked = image_sourcer._diversified_pexels_photos(photos, 4)

    from_same = [p for p in picked if p["photographer_id"] == "same"]
    assert len(from_same) == image_sourcer._PEXELS_MAX_PER_PHOTOGRAPHER
    assert {p["photographer_id"] for p in picked} == {"same", "b", "c"}


def test_a_thin_catalogue_still_fills_the_set():
    """The cap defers duplicates from one shoot; it does not discard them."""
    photos = [_photo(i, photographer="same") for i in range(1, 6)]

    picked = image_sourcer._diversified_pexels_photos(photos, 4)

    assert len(picked) == 4


# --- cross-beat deduplication --------------------------------------------


def _run_beat(client, keywords, output_path, monkeypatch):
    async def review_with_vision(prompt, image_paths, **kwargs):
        return {"approved": True, "winner_index": 1, "reason": "ok"}

    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", review_with_vision)
    return asyncio.run(
        image_sourcer._search_pexels(
            keywords,
            "a prompt",
            output_path,
            client,
            set(),
            target_size=(16, 9),
        )
    )


def test_a_photo_cannot_ship_on_two_beats(monkeypatch, tmp_path):
    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")
    photos = [_photo(1, alt="stadium floodlights"), _photo(2, alt="stadium crowd")]
    client = _LadderClient({"stadium night": photos})

    first = tmp_path / "section_001_01.jpg"
    second = tmp_path / "section_004_01.jpg"
    assert _run_beat(client, "stadium night", first, monkeypatch) is True
    assert _run_beat(client, "stadium night", second, monkeypatch) is True

    assert first.read_bytes() != second.read_bytes()
    assert image_sourcer._PEXELS_COMMITTED_IDS == {"1", "2"}


def test_photos_a_beat_looked_at_but_did_not_ship_are_released(monkeypatch, tmp_path):
    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")
    photos = [_photo(1, alt="stadium floodlights"), _photo(2, alt="stadium crowd")]
    client = _LadderClient({"stadium night": photos})

    _run_beat(client, "stadium night", tmp_path / "section_001_01.jpg", monkeypatch)

    # One shipped and is retired; the other is back in the pool for later beats.
    assert image_sourcer._PEXELS_COMMITTED_IDS == {"1"}
    assert image_sourcer._PEXELS_CLAIMED_IDS == set()


def test_a_rejected_beat_releases_every_photo_it_held(monkeypatch, tmp_path):
    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")
    client = _LadderClient({"stadium night": [_photo(1), _photo(2)]})

    async def review_with_vision(prompt, image_paths, **kwargs):
        return {"approved": False, "reason": "subject mismatch"}

    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", review_with_vision)
    result = asyncio.run(
        image_sourcer._search_pexels(
            "stadium night",
            "a prompt",
            tmp_path / "section_001_01.jpg",
            client,
            set(),
            target_size=(16, 9),
        )
    )

    assert result is False
    assert image_sourcer._PEXELS_CLAIMED_IDS == set()
    assert image_sourcer._PEXELS_COMMITTED_IDS == set()


# --- parallel search and weak results ------------------------------------


def test_every_rung_is_searched_in_both_orientations(monkeypatch, tmp_path):
    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")
    client = _LadderClient({})

    _collect(client, "a lone parachute over dense pine forest", tmp_path)

    ladder = image_sourcer.pexels_query_ladder(
        "a lone parachute over dense pine forest"
    )
    assert len(client.searches) == len(ladder) * 2
    assert {search["orientation"] for search in client.searches} == {
        "portrait", "landscape",
    }


def test_one_failing_query_does_not_cost_the_beat_its_picture(monkeypatch, tmp_path):
    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")
    ladder = image_sourcer.pexels_query_ladder("torn tent in snow")
    client = _LadderClient(
        {ladder[-1]: [_photo(9, alt="a torn tent in snow")]},
        failing_queries={ladder[0]},
    )

    records = _collect(client, "torn tent in snow", tmp_path)

    assert [record["pexels_id"] for record in records] == ["9"]


def test_every_query_failing_returns_nothing_rather_than_raising(monkeypatch, tmp_path):
    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")
    ladder = image_sourcer.pexels_query_ladder("torn tent in snow")
    client = _LadderClient({}, failing_queries=set(ladder))

    assert _collect(client, "torn tent in snow", tmp_path) == []


def test_results_are_pooled_across_the_whole_ladder(tmp_path):
    ladder = image_sourcer.pexels_query_ladder("torn tent in snow")
    client = _LadderClient({
        ladder[0]: [_photo(1, alt="a torn tent in snow")],
        ladder[-1]: [_photo(2, alt="snow")],
    })

    records = _collect(client, "torn tent in snow", tmp_path)

    assert {record["pexels_id"] for record in records} == {"1", "2"}


def test_the_same_photo_returned_by_two_rungs_is_counted_once(tmp_path):
    ladder = image_sourcer.pexels_query_ladder("torn tent in snow")
    shared = _photo(1, alt="a torn tent in snow")
    client = _LadderClient({query: [shared] for query in ladder})

    records = _collect(client, "torn tent in snow", tmp_path)

    assert len(records) == 1
