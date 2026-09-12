import React from "react";
import {
  AbsoluteFill,
  Sequence,
  OffthreadVideo,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
} from "remotion";
import type { ComponentType } from "react";
import { getTransition } from "../lib/transitions";
import { ImageScene } from "./ImageScene";
import { LocalAnimatedScene } from "./LocalAnimatedScene";
import { AnimatedLineChart } from "./AnimatedLineChart";
import { BackdropFigureScene } from "./BackdropFigureScene";
import { TitleCard } from "./TitleCard";
import { FactHighlight } from "./FactHighlight";
import { InfoCard } from "./InfoCard";
import { VerifiedCard } from "./VerifiedCard";
import { InfoSlide } from "./InfoSlide";
import { TextOnlySlide } from "./TextOnlySlide";
import { SubscribeCTA } from "./SubscribeCTA";
import { NarrationSubtitle } from "./NarrationSubtitle";
import { TitleBanner } from "./TitleBanner";
import { AnimatedBarChart } from "./AnimatedBarChart";
import { DonutGauge } from "./DonutGauge";
import { ComparisonBars } from "./ComparisonBars";
import { ChannelWatermark } from "./ChannelWatermark";
import type { WordTimestamp } from "../lib/types";

// ── Slot types ──────────────────────────────────────────────

interface ImageSlot {
  type: "image";
  imagePath: string;
  durationFrames: number;
  preset: string;
  direction: string;
}

interface ComponentSlot {
  type: "component";
  component: string;
  props: Record<string, unknown>;
  durationFrames: number;
}

interface VideoSlot {
  type: "video";
  videoPath: string;
  durationFrames: number;
}

type Slot = ImageSlot | ComponentSlot | VideoSlot;

interface TransitionSpec {
  type: string;
  durationFrames: number;
}

export interface WatermarkSpec {
  text?: string;
  logo_path?: string;
  opacity?: number;
  position?: "bottom_right" | "bottom_left" | "top_right" | "top_left";
}

export interface SectionCompositionProps {
  slots: Slot[];
  transition: TransitionSpec;
  narrationSubtitle?: {
    word_timestamps: WordTimestamp[];
    highlighted_keywords?: string[];
    highlight_color?: string;
    suppress_frame_ranges?: [number, number][];
  };
  watermark?: WatermarkSpec;
}

// ── Component registry ──────────────────────────────────────

/* eslint-disable @typescript-eslint/no-explicit-any */
const COMPONENTS: Record<string, ComponentType<any>> = {
  AnimatedLineChart,
  BackdropFigureScene,
  SubscribeCTA,
  TitleCard,
  FactHighlight,
  InfoCard,
  VerifiedCard,
  InfoSlide,
  TextOnlySlide,
  ImageScene,
  // Animated Stories' default scene renderer. Reached only when the Python
  // side attaches a motion recipe, so every other channel keeps the plain
  // `image` slot path unchanged.
  LocalAnimatedScene,
  NarrationSubtitle,
  TitleBanner,
  AnimatedBarChart,
  DonutGauge,
  ComparisonBars,
};

// ── Timing helpers ──────────────────────────────────────────

interface SlotTiming {
  startFrame: number;
  durationFrames: number;
}

function computeTimings(
  slots: Slot[],
  overlapFrames: number,
): SlotTiming[] {
  const timings: SlotTiming[] = [];
  let cursor = 0;
  for (let i = 0; i < slots.length; i++) {
    timings.push({ startFrame: cursor, durationFrames: slots[i].durationFrames });
    cursor += slots[i].durationFrames - (i < slots.length - 1 ? overlapFrames : 0);
  }
  return timings;
}

// ── Slot renderer ───────────────────────────────────────────

const SlotRenderer: React.FC<{ slot: Slot }> = ({ slot }) => {
  switch (slot.type) {
    case "image":
      return (
        <ImageScene
          image_path={slot.imagePath}
          animation_preset={slot.preset}
          direction={slot.direction}
        />
      );
    case "component": {
      const Comp = COMPONENTS[slot.component];
      if (!Comp) {
        throw new Error(`Unknown component: ${slot.component}`);
      }
      return <Comp {...slot.props} />;
    }
    case "video":
      return (
        <AbsoluteFill>
          <OffthreadVideo
            src={staticFile(slot.videoPath)}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        </AbsoluteFill>
      );
  }
};

// ── Transition wrapper ──────────────────────────────────────

const TransitionSlot: React.FC<{
  slot: Slot;
  transitionType: string;
  overlapFrames: number;
  isLast: boolean;
}> = ({ slot, transitionType, overlapFrames, isLast }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();

  // Exit transition: last overlapFrames of this slot (unless it's the last slot)
  let exitStyle: React.CSSProperties = {};
  if (!isLast && overlapFrames > 0 && transitionType !== "cut") {
    const exitStart = durationInFrames - overlapFrames;
    if (frame >= exitStart) {
      const progress = interpolate(
        frame,
        [exitStart, durationInFrames],
        [0, 1],
        { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
      );
      exitStyle = getTransition(transitionType, progress).exiting;
    }
  }

  // Enter transition: first overlapFrames of this slot (unless it's the first)
  // Note: the first slot check is handled by Sequence from=0, so frame 0 is
  // the first frame of THIS slot. We use the parent's Sequence to scope frames.
  let enterStyle: React.CSSProperties = {};
  if (overlapFrames > 0 && transitionType !== "cut" && frame < overlapFrames) {
    const progress = interpolate(
      frame,
      [0, overlapFrames],
      [0, 1],
      { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
    );
    enterStyle = getTransition(transitionType, progress).entering;
  }

  return (
    <AbsoluteFill style={{ ...enterStyle, ...exitStyle }}>
      <SlotRenderer slot={slot} />
    </AbsoluteFill>
  );
};

export const SectionComposition: React.FC<SectionCompositionProps> = ({
  slots,
  transition,
  narrationSubtitle,
  watermark,
}) => {
  const overlapFrames =
    transition.type === "cut" ? 0 : transition.durationFrames;
  const timings = computeTimings(slots, overlapFrames);

  return (
    <AbsoluteFill style={{ backgroundColor: "#000" }}>
      {/* Slot layers — earlier slots render below later slots in the overlap */}
      {slots.map((slot, i) => {
        const isFirst = i === 0;
        const isLast = i === slots.length - 1;
        return (
          <Sequence
            key={i}
            from={timings[i].startFrame}
            durationInFrames={slot.durationFrames}
          >
            <TransitionSlot
              slot={slot}
              transitionType={isFirst ? "cut" : transition.type}
              overlapFrames={isFirst ? 0 : overlapFrames}
              isLast={isLast}
            />
          </Sequence>
        );
      })}
      {/* Narration subtitle layer — spans entire section */}
      {narrationSubtitle && (
        <NarrationSubtitle
          word_timestamps={narrationSubtitle.word_timestamps}
          highlighted_keywords={narrationSubtitle.highlighted_keywords}
          highlight_color={narrationSubtitle.highlight_color}
          suppress_frame_ranges={narrationSubtitle.suppress_frame_ranges}
        />
      )}

      {/* Channel watermark — always visible on top of everything */}
      {watermark && <ChannelWatermark {...watermark} />}
    </AbsoluteFill>
  );
};
