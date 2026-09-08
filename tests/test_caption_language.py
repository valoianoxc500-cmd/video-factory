"""Captions in a language the narrator is not speaking.

The property that matters is sync. A caption line has to appear and leave when
its sentence is spoken, and the only measured thing available is the STT word
timings of the narration -- so these tests are mostly about spans: that a
translated line inherits its sentence's measured span, that the track stays
monotonic, and that every failure mode leaves the original captions in place
rather than an empty or shifted track.
"""

from __future__ import annotations

import asyncio

import pytest

from core import caption_language as cl


def words(*pairs) -> list[dict]:
    """(word, start, end) triples as the STT shape."""
    return [{"word": w, "start": s, "end": e} for w, s, e in pairs]


# ── sentence splitting ────────────────────────────────────────────────

def test_splits_on_arabic_question_mark():
    got = cl.split_sentences("اختفت العائلة. أين ذهبوا؟ لا أحد يعرف.")
    assert got == ["اختفت العائلة.", "أين ذهبوا؟", "لا أحد يعرف."]


def test_splits_on_latin_enders_and_normalises_whitespace():
    got = cl.split_sentences("He left.   Nobody saw him!  Why?")
    assert got == ["He left.", "Nobody saw him!", "Why?"]


def test_empty_narration_has_no_sentences():
    assert cl.split_sentences("   ") == []


# ── grouping measured timings onto sentences ─────────────────────────

def test_sentence_span_comes_from_its_own_words():
    rows = cl.group_words_into_sentences(
        words(("one", 0.0, 0.5), ("two", 0.5, 1.0), ("three", 1.2, 2.0)),
        ["one two.", "three."],
    )
    assert rows[0]["start"] == 0.0 and rows[0]["end"] == 1.0
    assert rows[1]["start"] == 1.2 and rows[1]["end"] == 2.0


def test_running_past_the_transcript_stays_monotonic():
    # Three sentences, but the transcript only covers the first.
    rows = cl.group_words_into_sentences(
        words(("one", 0.0, 0.4)),
        ["one.", "two.", "three."],
    )
    assert [r["end"] for r in rows] == [0.4, 0.4, 0.4]
    assert all(r["end"] >= r["start"] for r in rows)


def test_a_sentence_never_ends_before_it_starts():
    rows = cl.group_words_into_sentences(
        words(("a", 2.0, 1.0)),  # a malformed STT row
        ["a."],
    )
    assert rows[0]["end"] >= rows[0]["start"]


# ── the whole path ────────────────────────────────────────────────────

def build(words_in, narration, *, voice="ar", caption="en", translations=None):
    """Run build_caption_words with the translator stubbed."""
    async def fake_translate(sentences, **_):
        if translations is None:
            return [f"EN {i + 1}" for i in range(len(sentences))]
        return translations(sentences)

    original = cl.translate_sentences
    cl.translate_sentences = fake_translate
    try:
        return asyncio.run(
            cl.build_caption_words(
                words_in, narration, voice_language=voice, caption_language=caption
            )
        )
    finally:
        cl.translate_sentences = original


def test_matching_languages_leave_the_stt_timings_untouched():
    original = words(("مرحبا", 0.0, 0.5))
    got, translated = build(original, "مرحبا.", voice="ar", caption="ar")
    assert got is original
    assert translated is False


def test_translated_line_stays_inside_its_sentence_span():
    got, translated = build(
        words(("واحد", 0.0, 0.5), ("اثنان", 0.5, 1.0), ("ثلاثة", 3.0, 4.0)),
        "واحد اثنان. ثلاثة.",
        translations=lambda s: ["one two three four", "five"],
    )
    assert translated is True
    first = [w for w in got if w["end"] <= 1.0001]
    # The whole first translated sentence is laid inside [0.0, 1.0].
    assert len(first) == 4
    assert first[0]["start"] == pytest.approx(0.0)
    assert first[-1]["end"] == pytest.approx(1.0, abs=0.01)
    # The second sentence starts where the narrator actually starts it, not
    # where the first translation happened to run out.
    assert got[4]["start"] == pytest.approx(3.0)


def test_caption_track_is_monotonic():
    got, _ = build(
        words(("أ", 0.0, 1.0), ("ب", 1.0, 2.0), ("ج", 2.0, 3.0)),
        "أ ب. ج.",
        translations=lambda s: ["alpha beta gamma", "delta epsilon"],
    )
    for earlier, later in zip(got, got[1:]):
        assert later["start"] >= earlier["start"] - 1e-6
        assert earlier["end"] >= earlier["start"]


def test_a_failed_translation_keeps_the_spoken_captions():
    def boom(_sentences):
        raise RuntimeError("model refused")

    original = words(("واحد", 0.0, 0.5))
    got, translated = build(original, "واحد.", translations=boom)
    assert got is original
    assert translated is False


def test_a_wrong_sentence_count_is_rejected_rather_than_misaligned():
    # The real translator raises on a count mismatch; that must surface as a
    # fallback, never as captions shifted onto the wrong sentences.
    async def bad(sentences, **_):
        raise ValueError("returned 1 sentences for 2 inputs")

    original = words(("أ", 0.0, 1.0), ("ب", 1.0, 2.0))
    saved = cl.translate_sentences
    cl.translate_sentences = bad
    try:
        got, translated = asyncio.run(
            cl.build_caption_words(
                original, "أ. ب.", voice_language="ar", caption_language="en"
            )
        )
    finally:
        cl.translate_sentences = saved
    assert got is original
    assert translated is False


def test_unsupported_caption_language_falls_back():
    original = words(("أ", 0.0, 1.0))
    got, translated = build(original, "أ.", caption="zz")
    assert got is original
    assert translated is False


def test_no_words_means_nothing_to_align():
    got, translated = build([], "أ.", caption="en")
    assert got == []
    assert translated is False


def test_rtl_is_known_for_arabic_only():
    assert cl.is_rtl("ar") is True
    assert cl.is_rtl("en") is False


# ── the translator's own contract ────────────────────────────────────

def test_translator_rejects_a_count_mismatch():
    async def fake_json(prompt, **_):
        return {"translations": ["only one"]}

    saved = cl.clients.generate_json
    cl.clients.generate_json = fake_json
    try:
        with pytest.raises(ValueError, match="cannot align"):
            asyncio.run(
                cl.translate_sentences(
                    ["a.", "b."], source_language="ar", target_language="en"
                )
            )
    finally:
        cl.clients.generate_json = saved


def test_translator_rejects_an_empty_sentence():
    async def fake_json(prompt, **_):
        return {"translations": ["fine", "   "]}

    saved = cl.clients.generate_json
    cl.clients.generate_json = fake_json
    try:
        with pytest.raises(ValueError, match="empty"):
            asyncio.run(
                cl.translate_sentences(
                    ["a.", "b."], source_language="ar", target_language="en"
                )
            )
    finally:
        cl.clients.generate_json = saved
