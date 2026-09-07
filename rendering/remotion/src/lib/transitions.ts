import type React from "react";

export interface TransitionStyles {
  entering: React.CSSProperties;
  exiting: React.CSSProperties;
}

/**
 * Crossfade: incoming fades in over the outgoing slot.
 * The outgoing slot stays at full opacity underneath — since layers are
 * stacked (not composited), fading both would reveal the black background.
 */
export function fade(progress: number): TransitionStyles {
  return {
    entering: { opacity: progress },
    exiting: {},
  };
}

/**
 * Wipe left: incoming slides in from the right via clipPath.
 */
export function wipeleft(progress: number): TransitionStyles {
  const pct = progress * 100;
  return {
    entering: { clipPath: `inset(0 0 0 ${100 - pct}%)` },
    exiting: {},
  };
}

/**
 * Dissolve: a softer blend than `fade`.
 *
 * `fade` ramps opacity linearly, which on a photo cut reads as a visible
 * mechanical wipe-in. Dissolve eases the ramp and lets the incoming image
 * settle from very slightly oversized, so the change lands without a hard
 * edge — the look wanted for news/story beats between photographs.
 *
 * Deliberately NOT a port of FFmpeg's `dissolve`, which is a random-pixel
 * dissolve: that noise reads as harsh and dated on short-form video, and the
 * assembler maps its side of the boundary to a smooth blend for the same
 * reason. See `_TRANSITION_MAP` in core/assembler.py.
 */
export function dissolve(progress: number): TransitionStyles {
  // Smoothstep: slow at both ends, quickest through the middle.
  const eased = progress * progress * (3 - 2 * progress);
  return {
    entering: {
      opacity: eased,
      transform: `scale(${1.02 - 0.02 * eased})`,
    },
    exiting: {},
  };
}

/**
 * Hard cut: no overlap, no animation. Transition duration should be 0.
 */
export function cut(): TransitionStyles {
  return {
    entering: {},
    exiting: {},
  };
}

const TRANSITIONS: Record<string, (p: number) => TransitionStyles> = {
  fade,
  dissolve,
  wipeleft,
  cut: () => cut(),
};

/** Names this module can actually render. Mirrored by a Python-side guard test. */
export const SUPPORTED_TRANSITIONS = Object.keys(TRANSITIONS);

export function getTransition(
  type: string,
  progress: number,
): TransitionStyles {
  const fn = TRANSITIONS[type];
  if (!fn) {
    // Falling back silently is how `dissolve` sat in a channel's
    // transition_pool for a whole run while every boundary rendered a plain
    // fade. Say so; the render still completes.
    console.warn(
      `[transitions] unknown transition "${type}", falling back to fade. ` +
        `Supported: ${SUPPORTED_TRANSITIONS.join(", ")}`,
    );
    return fade(progress);
  }
  return fn(progress);
}
