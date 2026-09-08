"use strict";

/**
 * Caption highlight timing.
 *
 * final_review rejected a finished Horror render because "several frames are
 * missing the mandatory red highlight on the active word". The cause was that
 * each word was matched against its own [start, end] window while the chunk
 * containing it stayed on screen across the gaps between those windows, so
 * every inter-word pause rendered with no highlight anywhere.
 *
 * The fixture below is the real STT output from that run
 * (workspace/horror_stories_20260907_193204_530334_bac116), so the timings
 * that produced the rejection are the ones under test.
 */

const test = require("node:test");
const assert = require("node:assert");

const {
  activeChunkIndex,
  activeWordIndex,
  groupIntoChunks,
  isRtlText,
  HOLD_AFTER_CHUNK_SEC,
} = require("../.test-build/captionTiming.js");

const FPS = 30;

const AR = {
  fi: "في",
  aam: "عام",
  kaan: "كان",
  haris: "حارس",
  amn: "امن",
  yaqum: "يقوم",
  bidawriyatih: "بدوريته",
  almutad: "المعتاده",
  dakhil: "داخل",
  mustashfa: "مستشفى",
  mahjur: "مهجور",
  wamuzlim: "ومظلم",
  lilghaya: "للغايه",
  assabah: "الصباح",
};

/** Real word timings from the rejected render, section 1. */
const REAL_WORDS = [
  { word: AR.fi, start: 0.0, end: 0.56 },
  { word: AR.aam, start: 0.56, end: 1.4 },
  { word: "2006", start: 1.44, end: 2.88 },
  { word: AR.kaan, start: 4.04, end: 4.4 },
  { word: AR.haris, start: 4.4, end: 4.96 },
  { word: AR.amn, start: 4.96, end: 5.6 },
  { word: AR.yaqum, start: 5.6, end: 6.16 },
  { word: AR.bidawriyatih, start: 6.16, end: 7.12 },
  { word: AR.almutad, start: 7.12, end: 8.08 },
  { word: AR.dakhil, start: 8.72, end: 9.32 },
  { word: AR.mustashfa, start: 9.32, end: 10.08 },
  { word: AR.mahjur, start: 10.08, end: 11.04 },
  { word: AR.wamuzlim, start: 11.04, end: 11.84 },
  { word: AR.lilghaya, start: 11.84, end: 12.72 },
  { word: AR.fi, start: 14.08, end: 14.2 },
  { word: AR.assabah, start: 14.2, end: 14.76 },
];

/** The rule that shipped the rejected video, kept to prove the regression. */
function oldIsActive(word, currentSec) {
  return currentSec >= word.start && currentSec <= word.end;
}

/** Every frame at which a caption is on screen, with the chunk shown. */
function* visibleFrames(words, fps = FPS) {
  const chunks = groupIntoChunks(words);
  if (chunks.length === 0) return;
  const lastSec = chunks[chunks.length - 1].endSec + HOLD_AFTER_CHUNK_SEC;
  for (let frame = 0; frame <= Math.ceil(lastSec * fps) + 1; frame++) {
    const currentSec = frame / fps;
    const idx = activeChunkIndex(chunks, currentSec);
    if (idx !== -1) yield { frame, currentSec, chunk: chunks[idx] };
  }
}

// ── The invariant ────────────────────────────────────────────

test("every visible caption frame highlights exactly one word", () => {
  let frames = 0;
  for (const { chunk, currentSec, frame } of visibleFrames(REAL_WORDS)) {
    frames++;
    const idx = activeWordIndex(chunk, currentSec);
    assert.ok(
      Number.isInteger(idx) && idx >= 0 && idx < chunk.words.length,
      `frame ${frame} (${currentSec}s) highlighted no word`,
    );
  }
  assert.ok(frames > 400, `expected a full section of frames, got ${frames}`);
});

test("the old rule left this narration dark for whole runs", () => {
  // Guards the fixture: if this ever stops failing, the test below proves
  // nothing and the fixture has drifted away from the reported defect.
  let dark = 0;
  let longestRun = 0;
  let run = 0;
  for (const { chunk, currentSec } of visibleFrames(REAL_WORDS)) {
    const anyActive = chunk.words.some((w) => oldIsActive(w, currentSec));
    if (anyActive) {
      run = 0;
    } else {
      dark++;
      longestRun = Math.max(longestRun, ++run);
    }
  }
  assert.ok(dark > 50, `expected the reported defect, saw ${dark} dark frames`);
  assert.ok(
    longestRun >= 30,
    `expected a run over a second long, saw ${longestRun} frames`,
  );
});

test("a pause inside a chunk holds the highlight instead of dropping it", () => {
  // 12.72s -> 14.08s is silent, and the short-word rule keeps it inside one
  // chunk. This is the 1.3s stretch the reviewer saw with no red word.
  const chunks = groupIntoChunks(REAL_WORDS);
  const chunk = chunks.find(
    (c) => c.startSec <= 12.9 && c.endSec >= 14.1,
  );
  assert.ok(chunk, "fixture no longer spans the silent stretch");

  for (let t = 12.75; t < 14.07; t += 1 / FPS) {
    const idx = activeWordIndex(chunk, t);
    assert.strictEqual(
      chunk.words[idx].word,
      AR.lilghaya,
      `at ${t.toFixed(3)}s the last spoken word was not the one lit`,
    );
  }
});

// ── Synchronisation ──────────────────────────────────────────

test("a word is never lit before it is spoken", () => {
  for (const { chunk, currentSec } of visibleFrames(REAL_WORDS)) {
    const idx = activeWordIndex(chunk, currentSec);
    // Word 0 carries the chunk's opening frames, which can round a hair
    // early; any later word must actually have started.
    if (idx > 0) {
      assert.ok(
        currentSec >= chunk.words[idx].start,
        `${chunk.words[idx].word} lit at ${currentSec}s, before its start`,
      );
    }
  }
});

test("the highlight moves to a word on the frame it starts", () => {
  const chunk = groupIntoChunks(REAL_WORDS)[1];
  for (let i = 1; i < chunk.words.length; i++) {
    const startFrame = Math.round(chunk.words[i].start * FPS);
    assert.strictEqual(
      activeWordIndex(chunk, startFrame / FPS),
      i,
      `word ${i} was not lit at its own start frame`,
    );
    assert.strictEqual(
      activeWordIndex(chunk, (startFrame - 1) / FPS),
      i - 1,
      `word ${i} was lit a frame early`,
    );
  }
});

test("the highlight never moves backwards while a chunk is on screen", () => {
  const chunks = groupIntoChunks(REAL_WORDS);
  for (const chunk of chunks) {
    let previous = -1;
    for (
      let t = chunk.startSec;
      t <= chunk.endSec + HOLD_AFTER_CHUNK_SEC;
      t += 1 / FPS
    ) {
      const idx = activeWordIndex(chunk, t);
      assert.ok(idx >= previous, `highlight went backwards at ${t}s`);
      previous = idx;
    }
  }
});

test("the last word stays lit through the hold after the chunk ends", () => {
  const chunk = groupIntoChunks(REAL_WORDS)[0];
  const last = chunk.words.length - 1;
  const justAfter = chunk.endSec + HOLD_AFTER_CHUNK_SEC / 2;
  assert.strictEqual(activeWordIndex(chunk, justAfter), last);
});

test("out-of-order timestamps do not rewind the highlight", () => {
  const chunk = {
    words: [
      { word: "one", start: 0.0, end: 0.4 },
      { word: "two", start: 0.9, end: 1.2 },
      { word: "three", start: 0.5, end: 0.8 },
    ],
    startSec: 0,
    endSec: 1.2,
  };
  let previous = -1;
  for (let t = 0; t <= 1.4; t += 1 / FPS) {
    const idx = activeWordIndex(chunk, t);
    assert.ok(idx >= previous, `highlight went backwards at ${t}s`);
    previous = idx;
  }
});

test("a single-word chunk is always the lit word", () => {
  const chunk = {
    words: [{ word: "alone", start: 2.0, end: 2.3 }],
    startSec: 2.0,
    endSec: 2.3,
  };
  for (let t = 1.9; t <= 2.7; t += 1 / FPS) {
    assert.strictEqual(activeWordIndex(chunk, t), 0);
  }
});

// ── Chunk visibility, unchanged ──────────────────────────────

test("a chunk is held briefly after it ends, then the caption clears", () => {
  const chunks = groupIntoChunks(REAL_WORDS);
  const first = chunks[0];
  assert.strictEqual(activeChunkIndex(chunks, first.endSec + 0.1), 0);
  // Past the hold, the next chunk has not started: nothing is captioned.
  assert.strictEqual(
    activeChunkIndex(chunks, first.endSec + HOLD_AFTER_CHUNK_SEC + 0.1),
    -1,
  );
});

test("nothing is captioned before the first word", () => {
  const chunks = groupIntoChunks([{ word: "later", start: 5, end: 5.5 }]);
  assert.strictEqual(activeChunkIndex(chunks, 1.0), -1);
});

// ── Grouping and language handling, preserved ────────────────

test("chunks hold at most three words", () => {
  for (const chunk of groupIntoChunks(REAL_WORDS)) {
    assert.ok(chunk.words.length <= 3, "a chunk grew past three words");
    assert.ok(chunk.words.length >= 1);
  }
});

test("every word appears in exactly one chunk, in order", () => {
  const flat = groupIntoChunks(REAL_WORDS).flatMap((c) => c.words);
  assert.deepStrictEqual(flat, REAL_WORDS);
});

test("a long pause breaks the chunk before an ordinary word", () => {
  const chunks = groupIntoChunks([
    { word: "before", start: 0.0, end: 0.5 },
    { word: "after", start: 1.5, end: 2.0 },
  ]);
  assert.strictEqual(chunks.length, 2);
});

test("a very short word rides along rather than flashing alone", () => {
  const chunks = groupIntoChunks([
    { word: "before", start: 0.0, end: 0.5 },
    { word: "in", start: 1.5, end: 1.6 },
  ]);
  assert.strictEqual(chunks.length, 1);
});

test("no words produce no chunks", () => {
  assert.deepStrictEqual(groupIntoChunks([]), []);
});

test("Arabic is detected as right-to-left and Latin is not", () => {
  assert.ok(isRtlText(AR.mustashfa), "Arabic was not detected as RTL");
  assert.ok(isRtlText(REAL_WORDS.map((w) => w.word).join(" ")));
  assert.ok(!isRtlText("a security desk"), "Latin was treated as RTL");
  assert.ok(!isRtlText("2006"));
});
