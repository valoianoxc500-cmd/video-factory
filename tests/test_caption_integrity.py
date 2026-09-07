"""Arabic captions must never ship in Persian/Urdu script.

Every fixture here is the real drift from the Flight 19 / Bermuda Triangle
run: chirp_2, pinned to ar-EG, switched language right after the English
proper noun "تشارلز تيلور" and emitted 5.2 seconds of Urdu on screen.
"""

from __future__ import annotations

import pytest

from core.caption_integrity import (
    FOREIGN_LETTERS,
    contains_foreign_script,
    describe_drift,
    drift_ratio,
    drifted_indices,
    normalize_arabic,
    repair_word_timestamps,
)

# The narration actually spoken in section 1 of that video.
NARRATION = (
    "كان آخر اتصال لاسلكي من قائد السرب، الملازم تشارلز تيلور، مربكاً، "
    "حيث أبلغ عن ضياعه وأن بوصلاته لا تعمل بشكل صحيح."
)

# What the recognizer returned, verbatim, including the Urdu burst.
DRIFTED = [
    {"word": "كان", "start": 24.0, "end": 24.4},
    {"word": "آخر", "start": 24.7, "end": 25.1},
    {"word": "اتصال", "start": 25.4, "end": 26.0},
    {"word": "لاسلكي", "start": 26.2, "end": 26.9},
    {"word": "من", "start": 27.1, "end": 27.3},
    {"word": "قائد", "start": 27.5, "end": 28.0},
    {"word": "السرب", "start": 28.2, "end": 28.8},
    {"word": "الملازم", "start": 29.0, "end": 29.6},
    {"word": "تشارلز", "start": 29.7, "end": 30.0},
    # --- drift begins ---
    {"word": "اور", "start": 30.04, "end": 30.10},
    {"word": "بیکن", "start": 30.12, "end": 31.00},
    {"word": "ہائتو", "start": 31.04, "end": 31.36},
    {"word": "ابلگا", "start": 31.40, "end": 31.84},
    {"word": "انڈیا", "start": 31.88, "end": 32.36},
    {"word": "ہی", "start": 32.40, "end": 32.84},
    {"word": "وہ", "start": 32.88, "end": 33.80},
    {"word": "ہو", "start": 33.88, "end": 35.28},
    # --- drift ends ---
    {"word": "بشكل", "start": 35.40, "end": 35.90},
    {"word": "صحيح", "start": 36.00, "end": 36.60},
]

CLEAN = [
    {"word": "هل", "start": 0.0, "end": 0.3},
    {"word": "مثلث", "start": 0.4, "end": 0.9},
    {"word": "برمودا", "start": 1.0, "end": 1.6},
    {"word": "مجرد", "start": 1.7, "end": 2.1},
    {"word": "خرافه", "start": 2.2, "end": 2.9},   # STT orthography, still Arabic
]


# --- detection -------------------------------------------------------------

@pytest.mark.parametrize(
    "token", ["بیکن", "ہائتو", "ابلگا", "انڈیا", "ہی", "وہ", "ہو", "صحیح"]
)
def test_the_exact_drifted_tokens_are_detected(token):
    assert contains_foreign_script(token) is True


@pytest.mark.parametrize(
    "token",
    [
        "كان", "آخر", "اتصال", "لاسلكي", "الملازم", "تشارلز", "تيلور",
        "خرافه", "خرافة", "مربكاً", "بوصلاته", "صحيح",
        "الرحلة", "١٩", "19", "؟", "،",
    ],
)
def test_correct_arabic_is_never_flagged(token):
    """A false positive here would discard good captions."""
    assert contains_foreign_script(token) is False


def test_clean_transcript_reports_no_drift():
    assert drifted_indices(CLEAN) == []
    assert drift_ratio(CLEAN) == 0.0
    assert describe_drift(CLEAN) == "no script drift"


def test_the_real_drift_is_located_exactly():
    bad = drifted_indices(DRIFTED)
    # "اور" is Urdu but written in Arabic letters, so it is not script drift;
    # the seven tokens that use Perso-Arabic letters are.
    assert [DRIFTED[i]["word"] for i in bad] == [
        "بیکن", "ہائتو", "ابلگا", "انڈیا", "ہی", "وہ", "ہو",
    ]
    assert 0.3 < drift_ratio(DRIFTED) < 0.4


def test_drift_description_names_the_time_range():
    text = describe_drift(DRIFTED)
    assert "30.1s" in text and "35.3s" in text
    assert "بیکن" in text


def test_empty_input_is_safe():
    assert drifted_indices([]) == []
    assert drift_ratio([]) == 0.0
    assert contains_foreign_script("") is False


def test_foreign_letter_set_excludes_every_arabic_letter():
    for ch in "ابتثجحخدذرزسشصضطظعغفقكلمنهوي":
        assert ch not in FOREIGN_LETTERS, ch


# --- normalisation ---------------------------------------------------------

@pytest.mark.parametrize(
    "a,b",
    [
        ("خرافة", "خرافه"),      # ta marbuta vs heh
        ("أبلغ", "ابلغ"),          # hamza forms
        ("صحيح", "صحيح"),
        ("مربكاً", "مربكا"),      # diacritics
        ("تيلور", "تيلور"),
    ],
)
def test_orthography_variants_compare_equal(a, b):
    assert normalize_arabic(a) == normalize_arabic(b)


def test_distinct_words_do_not_collapse():
    assert normalize_arabic("قائد") != normalize_arabic("قاعد")


# --- repair ----------------------------------------------------------------

def test_repair_removes_every_drifted_token():
    repaired, replaced = repair_word_timestamps(DRIFTED, NARRATION)
    assert replaced == 7
    assert drifted_indices(repaired) == [], (
        "repaired captions still contain non-Arabic script"
    )


def test_repair_substitutes_the_real_narration_words():
    repaired, _ = repair_word_timestamps(DRIFTED, NARRATION)
    text = " ".join(w["word"] for w in repaired)
    # The words the speaker actually said in the corrupted window.
    for expected in ("ضياعه", "بوصلاته"):
        assert expected in text, f"{expected} missing from repaired captions"
    assert "انڈیا" not in text


def test_repair_preserves_arabic_punctuation():
    repaired, _ = repair_word_timestamps(DRIFTED, NARRATION)
    text = " ".join(w["word"] for w in repaired)
    assert "،" in text, "Arabic comma lost in repair"


def test_repair_keeps_timings_inside_the_drifted_window():
    repaired, _ = repair_word_timestamps(DRIFTED, NARRATION)
    assert all(w["end"] >= w["start"] for w in repaired)
    starts = [w["start"] for w in repaired]
    assert starts == sorted(starts), "repair reordered the timeline"
    # The clean tokens on either side keep their original timings.
    assert repaired[0]["start"] == 24.0
    assert repaired[-1]["end"] == 36.6


def test_repair_is_a_no_op_on_clean_captions():
    repaired, replaced = repair_word_timestamps(CLEAN, NARRATION)
    assert replaced == 0
    assert repaired == CLEAN


def test_a_wholly_drifted_transcript_is_replaced_by_the_narration():
    """With no clean token to anchor on, the whole span becomes the narration.

    This is the degenerate case of the same rule, and it is what should
    happen: the audio is a reading of the narration, so the narration is
    always the correct caption text.
    """
    narration = "نص مختلف تماما"
    wholly_drifted = [
        {"word": "ہو", "start": 1.0, "end": 2.0},
        {"word": "ہی", "start": 2.0, "end": 3.0},
    ]
    repaired, replaced = repair_word_timestamps(wholly_drifted, narration)
    assert replaced == 2
    assert drifted_indices(repaired) == []
    assert " ".join(w["word"] for w in repaired) == narration
    # And it stays inside the original window.
    assert repaired[0]["start"] == 1.0
    assert repaired[-1]["end"] <= 3.0


def test_repair_never_leaves_foreign_script_behind():
    """The guarantee the whole guard exists to make."""
    for words in (DRIFTED, [{"word": "ابلگا", "start": 0.0, "end": 1.0}]):
        repaired, _ = repair_word_timestamps(words, NARRATION)
        assert drifted_indices(repaired) == []


def test_repair_without_narration_is_safe():
    _, replaced = repair_word_timestamps(DRIFTED, "")
    assert replaced == 0
