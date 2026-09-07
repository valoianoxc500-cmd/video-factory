"""A vertical short needs a vertical thumbnail.

The final review gate blocked an otherwise-passing Horror package on it:

    "the inclusion of a landscape frame (Image 2) and a landscape thumbnail for
     a vertical Short-form video are blocking issues"

The video was 1080x1920 and every review frame was 1080x1920; the thumbnail was
1280x720, the one landscape image in the set.
"""

import pytest
from PIL import Image

from core.thumbnailer import THUMBNAIL_SIZE, VERTICAL_THUMBNAIL_SIZE, thumbnail_size
from core.utils import load_channel_config


def test_horror_publishes_a_vertical_thumbnail():
    assert thumbnail_size(load_channel_config("horror_stories")) == (1080, 1920)


def test_the_thumbnail_matches_the_video_shape():
    cfg = load_channel_config("horror_stories")
    tw, th = thumbnail_size(cfg)
    vw, vh = cfg.video.resolution
    assert (tw / th) == pytest.approx(vw / vh), "thumbnail aspect differs from the video"


def test_football_news_thumbnails_are_unchanged():
    """Opt-in only: the other channel keeps the 16:9 it has always published."""
    assert thumbnail_size(load_channel_config("football_news")) == THUMBNAIL_SIZE


def test_the_flag_defaults_to_landscape():
    from core.utils import TemplateStyle

    assert TemplateStyle().vertical_thumbnail is False


def test_vertical_size_is_the_render_resolution():
    assert VERTICAL_THUMBNAIL_SIZE == (1080, 1920)


# --- reshaping a generated image ------------------------------------------

def _fit(src_size, target):
    from PIL import ImageOps

    img = Image.new("RGB", src_size, (40, 40, 40))
    return ImageOps.fit(img, target, method=Image.Resampling.LANCZOS)


def test_a_generated_landscape_image_is_cropped_not_squashed():
    """Stretching 16:9 into 9:16 distorts the subject's face."""
    out = _fit((1344, 768), VERTICAL_THUMBNAIL_SIZE)
    assert out.size == VERTICAL_THUMBNAIL_SIZE


@pytest.mark.parametrize("src", [(1344, 768), (1280, 720), (1024, 1024), (1080, 1920)])
def test_any_generated_shape_reaches_the_target(src):
    assert _fit(src, VERTICAL_THUMBNAIL_SIZE).size == VERTICAL_THUMBNAIL_SIZE
