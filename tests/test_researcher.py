"""Guards on the research brief that reaches the scripter.

Grounded search returning citations is not the same as the citations being
real, current, or reputable. These cover the two structural checks that run
before a brief is allowed to become narration.
"""

import io
from datetime import date, timedelta

import pytest
from PIL import Image

from core import image_sourcer, processor, researcher


# ── Fabricated research ──────────────────────────────────────────


def test_far_future_dated_research_is_rejected():
    """A brief dated well after today describes events that have not happened.

    A live run came back "grounded" with six COMPLETED transfers dated ahead of
    time, sourced from content farms, and the pipeline narrated them as
    breaking news.
    """
    reason = researcher._reject_fabricated_research({
        "as_of_date": (date.today() + timedelta(days=5)).isoformat(),
        "sources": [{"url": "https://www.skysports.com/x"}],
    })
    assert "ahead of today" in reason.lower()


def test_tomorrow_is_allowed_for_timezone_skew():
    """One day ahead is UTC skew, not fabrication.

    The worker's clock is local while the model reports in UTC, so for the
    hours around UTC midnight a current source reads as tomorrow. A zero-slack
    check discarded good research every evening -- a 20:12 local run was
    already 03:12 UTC the next day.
    """
    reason = researcher._reject_fabricated_research({
        "as_of_date": (date.today() + timedelta(days=1)).isoformat(),
        "sources": [{"url": "https://www.skysports.com/x"}],
    })
    assert reason == ""


def test_today_dated_research_is_kept():
    reason = researcher._reject_fabricated_research({
        "as_of_date": date.today().isoformat(),
        "sources": [{"url": "https://www.bbc.co.uk/sport/football/123"}],
    })
    assert reason == ""


def test_research_without_a_reputable_source_is_rejected():
    reason = researcher._reject_fabricated_research({
        "as_of_date": date.today().isoformat(),
        "sources": [
            {"url": "https://sportpesa.com/news/x"},
            {"url": "https://stadiounited.com/y"},
        ],
    })
    assert "recognised football news outlet" in reason


def test_one_reputable_source_is_enough():
    reason = researcher._reject_fabricated_research({
        "as_of_date": date.today().isoformat(),
        "sources": [
            {"url": "https://stadiounited.com/y"},
            {"url": "https://www.premierleague.com/news/123"},
        ],
    })
    assert reason == ""


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://www.bbc.co.uk/sport", "www.bbc.co.uk"),
        ("skysports.com", "skysports.com"),
        ("http://espn.com/soccer/match?id=1", "espn.com"),
    ],
)
def test_source_domains_parses_urls_and_bare_domains(raw, expected):
    assert researcher._source_domains({"sources": [raw]}) == [expected]


def test_subdomains_of_trusted_outlets_count():
    assert researcher._trusted_source_count(
        {"sources": ["https://www.espn.co.uk/football/x"]}
    ) == 1


def test_lookalike_domain_does_not_count():
    """Suffix matching must not accept a domain that merely ends in the text."""
    assert researcher._trusted_source_count(
        {"sources": ["https://notbbc.co.uk.example.net/x"]}
    ) == 0


def test_no_sources_at_all_is_not_treated_as_untrusted():
    """Absent sources are handled by the grounded check, not this one."""
    assert researcher._reject_fabricated_research({"as_of_date": ""}) == ""


# ── 9:16 reframing ───────────────────────────────────────────────

TARGET = (1080, 1920)


def _fill_fraction(img: Image.Image) -> float:
    """Share of rows that are not part of a uniform blurred bar.

    A letterboxed frame has wide bands of near-constant colour top and bottom;
    a cover-cropped one does not.
    """
    small = img.resize((64, 114))
    rows = [
        [small.getpixel((x, y)) for x in range(64)]
        for y in range(114)
    ]
    varied = sum(1 for r in rows if max(max(p) - min(p) for p in r) > 12)
    return varied / len(rows)


def test_landscape_photo_fills_the_vertical_frame():
    """16:9 must cover-crop, not letterbox.

    Letterboxed into 1080x1920 a 16:9 photo occupies about a fifth of the
    height with blurred bars over the rest, and the final-review gate rejects
    it as "not native vertical video".
    """
    src = Image.new("RGB", (1747, 980))
    # Horizontal gradient, so a cover crop still yields varied rows.
    for x in range(1747):
        for y in range(0, 980, 20):
            src.paste((x % 256, 90, 200 - x % 150), (x, y, x + 1, min(y + 20, 980)))

    out = processor._fit_to_canvas(src, TARGET)

    assert out.size == TARGET
    assert _fill_fraction(out) > 0.9, "16:9 source was letterboxed instead of cropped"


def test_extreme_panorama_still_uses_the_blurred_surround():
    """Cropping a 3:1 panorama to 9:16 would discard the subject entirely."""
    src = Image.new("RGB", (3000, 1000), (30, 120, 60))
    out = processor._fit_to_canvas(src, TARGET)
    assert out.size == TARGET


def test_matching_aspect_ratio_is_resized_untouched():
    src = Image.new("RGB", (540, 960), (12, 16, 24))
    out = processor._fit_to_canvas(src, TARGET)
    assert out.size == TARGET


def test_portrait_source_is_not_distorted():
    """A taller-than-target source crops vertically rather than stretching."""
    src = Image.new("RGB", (1080, 2400), (200, 40, 40))
    out = processor._fit_to_canvas(src, TARGET)
    assert out.size == TARGET


# ── Sourcing-time reframe ────────────────────────────────────────
#
# The sourcer normalises every downloaded photo to the target size before the
# processor ever sees it, so a letterbox introduced here is baked in and no
# later stage can undo it. That is where the blurred bars actually came from.


def _encode(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_sourced_landscape_photo_is_cropped_not_letterboxed():
    src = Image.new("RGB", (1747, 980))
    for x in range(1747):
        src.paste((x % 256, 90, 200 - x % 150), (x, 0, x + 1, 980))

    out = Image.open(
        io.BytesIO(
            image_sourcer._normalize_photo_bytes_for_target(
                _encode(src), target_size=TARGET
            )
        )
    ).convert("RGB")

    assert out.size == TARGET
    assert _fill_fraction(out) > 0.9, (
        "sourced 16:9 photo was letterboxed; the bars are baked in before "
        "the processor can fix them"
    )


def test_sourced_extreme_panorama_keeps_the_surround():
    src = Image.new("RGB", (3000, 1000), (30, 120, 60))
    out = Image.open(
        io.BytesIO(
            image_sourcer._normalize_photo_bytes_for_target(
                _encode(src), target_size=TARGET
            )
        )
    ).convert("RGB")
    assert out.size == TARGET
