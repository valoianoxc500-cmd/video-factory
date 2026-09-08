/**
 * Caption grouping and active-word selection.
 *
 * Pure timing arithmetic, deliberately free of React and Remotion imports so
 * the highlight invariant can be unit-tested frame by frame without a
 * renderer. `NarrationSubtitle` is the only consumer; it supplies the current
 * time and does the drawing.
 */

/** Word-level timestamp from STT, in seconds from the start of the section. */
export interface WordTimestamp {
  word: string;
  start: number;
  end: number;
}

/**
 * A short group of words shown together, with one of them active.
 *
 * Captions are cut into 1-3 word chunks rather than sentences: a full line of
 * Arabic is unreadable at a glance on a phone, and the point of the format is
 * that the eye lands on the word being spoken right now.
 */
export interface Chunk {
  words: WordTimestamp[];
  startSec: number;
  endSec: number;
}

// ── Grouping ─────────────────────────────────────────────────

export const MAX_WORDS_PER_CHUNK = 3;
/** A pause longer than this ends the chunk early — it reads as a beat. */
export const GAP_THRESHOLD_SEC = 0.28;
/** Very short words ride along with a neighbour instead of flashing alone. */
export const SHORT_WORD_CHARS = 3;
/** How long a finished chunk lingers before the caption clears. */
export const HOLD_AFTER_CHUNK_SEC = 0.4;

// Arabic / Hebrew / Thaana ranges -- enough to detect right-to-left narration.
const RTL_RE = /[֐-׿؀-ۿ܀-ݏހ-޿ࢠ-ࣿיִ-﷿ﹰ-﻿]/;

export function isRtlText(text: string): boolean {
  return RTL_RE.test(text);
}

export function groupIntoChunks(words: WordTimestamp[]): Chunk[] {
  if (words.length === 0) return [];

  const chunks: Chunk[] = [];
  let buf: WordTimestamp[] = [];

  const flush = () => {
    if (buf.length === 0) return;
    chunks.push({
      words: buf,
      startSec: buf[0].start,
      endSec: buf[buf.length - 1].end,
    });
    buf = [];
  };

  for (let i = 0; i < words.length; i++) {
    const cur = words[i];
    const prev = words[i - 1];

    if (buf.length > 0 && prev) {
      const gap = cur.start - prev.end;
      // Keep a chunk to at most three words, and break on a real pause so the
      // grouping follows the delivery rather than fighting it.
      const wouldOverflow = buf.length >= MAX_WORDS_PER_CHUNK;
      const longPause = gap > GAP_THRESHOLD_SEC;
      // A lone very short word (Arabic في, من, على) looks like a glitch, so
      // only break before one if the chunk is already full.
      const curIsShort = cur.word.replace(/\W/g, "").length <= SHORT_WORD_CHARS;
      if (wouldOverflow || (longPause && !curIsShort)) flush();
    }
    buf.push(cur);
  }
  flush();

  return chunks;
}

// ── Selection ────────────────────────────────────────────────

/**
 * Which chunk is on screen at `currentSec`, or -1 for none.
 *
 * Outside any chunk's window this holds the chunk that just ended, briefly, so
 * a pause between chunks does not blink the caption off. Past that hold the
 * caption clears, so trailing silence is not captioned.
 */
export function activeChunkIndex(chunks: Chunk[], currentSec: number): number {
  const containing = chunks.findIndex(
    (c) => currentSec >= c.startSec && currentSec <= c.endSec,
  );
  if (containing !== -1) return containing;

  for (let i = chunks.length - 1; i >= 0; i--) {
    if (currentSec > chunks[i].endSec) {
      return currentSec - chunks[i].endSec < HOLD_AFTER_CHUNK_SEC ? i : -1;
    }
  }
  return -1;
}

/**
 * Which word of a visible chunk carries the highlight at `currentSec`.
 *
 * Always returns a real index. That is the point: matching each word against
 * its own `[start, end]` left every inter-word gap with nothing highlighted,
 * and a chunk stays on screen across those gaps. On real narration that was
 * 15% of caption frames rendered with no highlight at all, in runs of over a
 * second wherever a pause fell inside a chunk -- which the short-word rule in
 * `groupIntoChunks` makes routine. `final_review` rejected the video for it.
 *
 * So the words partition the chunk instead of sampling it: word `i` lights at
 * its own `start` and holds until word `i+1` starts. A word never lights
 * early, so the highlight stays in sync with the narration; a pause simply
 * keeps the last word spoken lit rather than going dark.
 *
 * `break` rather than `continue` on the first word that has not started keeps
 * the highlight moving forward even if timestamps arrive out of order.
 */
export function activeWordIndex(chunk: Chunk, currentSec: number): number {
  let index = 0;
  for (let i = 0; i < chunk.words.length; i++) {
    if (currentSec >= chunk.words[i].start) index = i;
    else break;
  }
  return index;
}
