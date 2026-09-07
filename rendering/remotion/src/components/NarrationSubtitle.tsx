import React from "react";
import {
  AbsoluteFill,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  spring,
} from "remotion";
import { theme } from "../design/theme";

// ── Types ────────────────────────────────────────────────────

export interface WordTimestamp {
  word: string;
  start: number;
  end: number;
}

export interface NarrationSubtitleProps {
  word_timestamps: WordTimestamp[];
  highlighted_keywords?: string[];
  highlight_color?: string;
  /** Frame ranges [start, end] where subtitles should be hidden (e.g. during chart slots). */
  suppress_frame_ranges?: [number, number][];
}

/**
 * A short group of words shown together, with one of them active.
 *
 * Captions are cut into 1-3 word chunks rather than sentences: a full line of
 * Arabic is unreadable at a glance on a phone, and the point of the format is
 * that the eye lands on the word being spoken right now.
 */
interface Chunk {
  words: WordTimestamp[];
  startSec: number;
  endSec: number;
}

// ── Grouping ─────────────────────────────────────────────────

const MAX_WORDS_PER_CHUNK = 3;
/** A pause longer than this ends the chunk early — it reads as a beat. */
const GAP_THRESHOLD_SEC = 0.28;
/** Very short words ride along with a neighbour instead of flashing alone. */
const SHORT_WORD_CHARS = 3;

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

// ── Component ────────────────────────────────────────────────

const ACTIVE_COLOR = "#FFE500";
const IDLE_COLOR = "#FFFFFF";
/** Horizontal room captions may use, leaving a margin on both sides. */
const SAFE_WIDTH_FRACTION = 0.86;
/** Space between words, in em. */
const WORD_GAP_EM = 0.42;
/** Never shrink below this fraction of the base size. */
const MIN_FONT_SCALE = 0.55;

/** Heavy 8-direction outline so text survives any photograph behind it. */
function strokeShadow(px: number): string {
  const offsets: [number, number][] = [
    [-1, -1], [0, -1], [1, -1],
    [-1, 0], [1, 0],
    [-1, 1], [0, 1], [1, 1],
  ];
  return [
    ...offsets.map(([x, y]) => `${x * px}px ${y * px}px 0 #000`),
    "0 6px 18px rgba(0,0,0,0.85)",
  ].join(", ");
}

export const NarrationSubtitle: React.FC<NarrationSubtitleProps> = ({
  word_timestamps,
  highlight_color = ACTIVE_COLOR,
  suppress_frame_ranges = [],
}) => {
  const frame = useCurrentFrame();
  const { fps, width, height } = useVideoConfig();

  const chunks = React.useMemo(
    () => groupIntoChunks(word_timestamps ?? []),
    [word_timestamps],
  );

  const isSuppressed = suppress_frame_ranges.some(
    ([start, end]) => frame >= start && frame < end,
  );
  if (isSuppressed) return null;

  const currentSec = frame / fps;

  // The chunk whose window contains now; otherwise the last one that started,
  // so a gap between words holds the previous caption instead of blinking off.
  let activeIdx = chunks.findIndex(
    (c) => currentSec >= c.startSec && currentSec <= c.endSec,
  );
  if (activeIdx === -1) {
    for (let i = chunks.length - 1; i >= 0; i--) {
      if (currentSec > chunks[i].endSec) {
        // Only hold briefly, so trailing silence is not captioned.
        if (currentSec - chunks[i].endSec < 0.4) activeIdx = i;
        break;
      }
    }
  }
  if (activeIdx === -1) return null;

  const chunk = chunks[activeIdx];
  const rtl = isRtlText(chunk.words.map((w) => w.word).join(" "));

  const chunkStartFrame = Math.round(chunk.startSec * fps);
  const localFrame = frame - chunkStartFrame;

  // Chunk entrance: a quick spring so each group lands with some snap.
  const enter = spring({
    frame: localFrame,
    fps,
    config: { damping: 24, stiffness: 220, mass: 0.5 },
    durationInFrames: 8,
  });

  // Sizing is relative to the frame so it reads the same at any resolution.
  const baseFontSize = width * (rtl ? 0.098 : 0.09);

  // Shrink long chunks so they cannot run past the edges. Three long Arabic
  // words at the base size overflow 1080px and get clipped mid-word, which
  // looks broken. Width is estimated from the glyph count rather than measured
  // so this stays a pure render with no layout pass.
  const usableWidth = width * SAFE_WIDTH_FRACTION;
  const glyphs = chunk.words.reduce((n, w) => n + w.word.length, 0);
  const gapCount = Math.max(chunk.words.length - 1, 0);
  // Average advance width per glyph, as a fraction of the font size.
  const advance = rtl ? 0.52 : 0.58;
  const estimatedWidth =
    baseFontSize * (glyphs * advance + gapCount * WORD_GAP_EM);
  const fitScale =
    estimatedWidth > usableWidth ? usableWidth / estimatedWidth : 1;

  const fontSize = Math.round(
    Math.max(baseFontSize * MIN_FONT_SCALE, baseFontSize * fitScale),
  );
  const strokeWidth = Math.max(3, Math.round(fontSize * 0.055));

  // Lower-middle safe area: clear of the very bottom, where phone UI and
  // platform chrome (Reels/TikTok captions, progress bars) sit.
  const bottomInset = Math.round(height * 0.19);

  return (
    <AbsoluteFill
      style={{
        display: "flex",
        justifyContent: "flex-end",
        alignItems: "center",
        paddingBottom: bottomInset,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          opacity: enter,
          transform: `translateY(${interpolate(enter, [0, 1], [18, 0])}px)`,
          display: "flex",
          // `direction: rtl` already lays flex items out right-to-left.
          // Adding row-reverse on top of it would flip them back and print
          // the words in the wrong order.
          flexDirection: "row",
          // Wrapping is the backstop: if the width estimate is off,
          // the caption breaks to a second line rather than clipping.
          flexWrap: "wrap",
          justifyContent: "center",
          alignItems: "baseline",
          gap: `${Math.round(fontSize * WORD_GAP_EM)}px`,
          maxWidth: `${SAFE_WIDTH_FRACTION * 100}%`,
          direction: rtl ? "rtl" : "ltr",
          textAlign: "center",
        }}
      >
        {chunk.words.map((w, i) => {
          const isActive = currentSec >= w.start && currentSec <= w.end;
          // Pop the word as it becomes active, then settle back.
          const wordLocal = frame - Math.round(w.start * fps);
          const pop = isActive
            ? spring({
                frame: wordLocal,
                fps,
                config: { damping: 12, stiffness: 320, mass: 0.4 },
                durationInFrames: 7,
              })
            : 0;
          const scale = 1 + 0.16 * (isActive ? 1 - Math.abs(1 - pop) : 0);

          return (
            <span
              key={`${w.word}-${i}-${w.start}`}
              style={{
                display: "inline-block",
                transform: `scale(${scale})`,
                color: isActive ? highlight_color : IDLE_COLOR,
                fontFamily: rtl ? theme.font.arabic : theme.font.sans,
                fontSize,
                fontWeight: 900,
                lineHeight: 1.18,
                // Arabic is cursive; letter spacing breaks the joins.
                letterSpacing: rtl ? 0 : 0.5,
                textTransform: rtl ? "none" : "uppercase",
                unicodeBidi: "plaintext",
                textShadow: strokeShadow(strokeWidth),
                whiteSpace: "pre",
              }}
            >
              {w.word}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
