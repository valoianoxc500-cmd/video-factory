import React from "react";
import {
  AbsoluteFill,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  spring,
} from "remotion";
import { theme } from "../design/theme";
import {
  activeChunkIndex,
  activeWordIndex,
  groupIntoChunks,
  isRtlText,
  type WordTimestamp,
} from "../lib/captionTiming";

// Grouping and active-word selection live in ../lib/captionTiming so the
// highlight invariant can be tested frame by frame without a renderer.
// Re-exported here because this module was their original home.
export { groupIntoChunks, isRtlText };
export type { WordTimestamp };

// ── Types ────────────────────────────────────────────────────

export interface NarrationSubtitleProps {
  word_timestamps: WordTimestamp[];
  highlighted_keywords?: string[];
  highlight_color?: string;
  /** Frame ranges [start, end] where subtitles should be hidden (e.g. during chart slots). */
  suppress_frame_ranges?: [number, number][];
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

  const activeIdx = activeChunkIndex(chunks, currentSec);
  if (activeIdx === -1) return null;

  const chunk = chunks[activeIdx];
  // Exactly one word of a visible chunk is highlighted, on every frame.
  const activeWord = activeWordIndex(chunk, currentSec);
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
          const isActive = i === activeWord;
          // Pop the word as it becomes active, then settle back. Keyed on the
          // word's own start, so the pop still lands on the spoken onset even
          // though the highlight now holds through any pause that follows.
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
