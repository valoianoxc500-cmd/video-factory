"""Tests for subject-aware reframing."""

from PIL import Image

from core.framing import (
    MIN_COVER_RETENTION,
    _dominant_faces,
    find_subject,
    subject_aware_fit,
    verticality_score,
)

TARGET = (1080, 1920)


def _canvas(width: int, height: int, color=(20, 20, 20)) -> Image.Image:
    return Image.new("RGB", (width, height), color)


def test_fit_always_returns_exact_target_size():
    for size in [(3000, 2000), (800, 1400), (1920, 1080), (1080, 1920), (4000, 900)]:
        out = subject_aware_fit(_canvas(*size), TARGET)
        assert out.size == TARGET, f"{size} produced {out.size}"


def test_matching_aspect_ratio_is_only_resized():
    out = subject_aware_fit(_canvas(540, 960), TARGET)
    assert out.size == TARGET


def test_panorama_falls_back_to_blurred_surround():
    # 10:1 retains ~0.056, far below the cover-crop floor, so the whole frame
    # is kept over a blur rather than cropped down to a slice.
    wide = _canvas(4000, 400)
    assert verticality_score(4000, 400, TARGET) < MIN_COVER_RETENTION
    assert subject_aware_fit(wide, TARGET).size == TARGET


def test_verticality_score_prefers_portrait_sources():
    portrait = verticality_score(1080, 1920, TARGET)
    landscape = verticality_score(1920, 1080, TARGET)
    panorama = verticality_score(4000, 500, TARGET)
    assert portrait == 1.0
    assert portrait > landscape > panorama
    assert 0.3 < landscape < 0.35  # a 16:9 source keeps about a third


def test_verticality_score_handles_degenerate_sizes():
    assert verticality_score(0, 100, TARGET) == 0.0
    assert verticality_score(100, 0, TARGET) == 0.0


def test_subject_defaults_to_center_when_nothing_is_detectable():
    flat = _canvas(1600, 900)  # uniform fill: no faces, no edges
    (x, y), detector = find_subject(flat)
    assert detector == "center"
    assert (x, y) == (800.0, 450.0)


def test_saliency_pulls_focus_toward_image_detail():
    # Detail confined to the left third should move the focus point left of
    # center, which is what stops a stadium or document being cropped blindly.
    img = _canvas(1600, 900)
    for x in range(80, 400, 8):
        for y in range(200, 700, 8):
            img.putpixel((x, y), (255, 255, 255))
    (x, _), detector = find_subject(img)
    assert detector == "saliency"
    assert x < 800


def test_dominant_faces_drops_background_bystanders():
    subject = (500, 300, 200, 200)          # large foreground face
    crowd = [(10, 10, 20, 20), (60, 12, 18, 18), (110, 8, 22, 22)]
    kept = _dominant_faces([subject, *crowd])
    assert kept == [subject]


def test_dominant_faces_keeps_comparable_subjects():
    # Two players in a duel are both the subject and must both survive.
    a = (200, 200, 180, 180)
    b = (700, 210, 170, 170)
    kept = _dominant_faces([a, b])
    assert set(kept) == {a, b}


def test_dominant_faces_on_empty_input():
    assert _dominant_faces([]) == []


def test_crop_window_stays_inside_the_source():
    # A subject hard against the right edge must not produce a window that
    # runs past the image bounds and pads with empty pixels.
    img = _canvas(2000, 1000)
    for x in range(1900, 2000):
        for y in range(400, 600):
            img.putpixel((x, y), (255, 255, 255))
    out = subject_aware_fit(img, TARGET)
    assert out.size == TARGET
