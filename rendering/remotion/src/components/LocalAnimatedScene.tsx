import React from "react";
import {
  AbsoluteFill,
  Img,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

/**
 * A still, animated locally and for nothing.
 *
 * This is the default scene renderer for Animated Stories. It layers four
 * independent treatments over one generated image:
 *
 *   camera    the frame moves        (pan, push, drift, handheld)
 *   subject   the image micro-moves  (breathe, sway, lean, recoil)
 *   effects   things fall or rise    (rain, snow, fire, smoke, sparks, dust, fog)
 *   lighting  the light changes      (flicker, sweep, pulse)
 *
 * Camera and subject are deliberately separate nested transforms. Applying
 * both to one element makes them cancel at the midpoint of a beat and the
 * scene looks locked; nesting keeps the subject alive inside a moving frame.
 *
 * Every value is a pure function of `frame` and the recipe's `seed`. Remotion
 * renders frames out of order and retries them, so anything drawn from
 * `Math.random()` would flicker between passes. `rand()` below is a seeded
 * integer hash for exactly that reason.
 *
 * What this cannot do is move limbs. A walk cycle needs the pixels to move
 * relative to each other and no transform here produces that; those beats are
 * the ones the Python side escalates to the paid model.
 */

export interface LocalMotionRecipe {
  camera?: string;
  direction?: string;
  intensity?: number;
  subject?: string;
  effects?: string[];
  lighting?: string;
  seed?: number;
}

export interface LocalAnimatedSceneProps {
  image_path: string;
  motion?: LocalMotionRecipe;
}

/** Deterministic 0..1 from two integers. No state, no clock. */
function rand(seed: number, n: number): number {
  let x = (seed + n * 374761393) & 0xffffffff;
  x = (x ^ (x >>> 13)) * 1274126177;
  x = x ^ (x >>> 16);
  return ((x >>> 0) % 100000) / 100000;
}

const easeInOut = (t: number) => (t < 0.5 ? 2 * t * t : -1 + (4 - 2 * t) * t);
const lerp = (t: number, a: number, b: number) => a + (b - a) * t;

export const LocalAnimatedScene: React.FC<LocalAnimatedSceneProps> = ({
  image_path,
  motion = {},
}) => {
  const frame = useCurrentFrame();
  const { durationInFrames, fps, width, height } = useVideoConfig();

  const progress = frame / Math.max(durationInFrames - 1, 1);
  const seconds = frame / Math.max(fps, 1);
  const intensity = clamp(motion.intensity ?? 0.5, 0, 1);
  const seed = motion.seed ?? 0;

  const camera = cameraStyle(
    motion.camera ?? "parallax",
    motion.direction ?? "left",
    progress,
    intensity,
  );
  const subject = subjectStyle(motion.subject ?? "breathe", seconds, intensity);

  return (
    <AbsoluteFill style={{ overflow: "hidden", backgroundColor: "#05070a" }}>
      {/* camera */}
      <AbsoluteFill style={camera}>
        {/* subject, alive inside the moving frame */}
        <Img
          src={staticFile(image_path)}
          style={{
            position: "absolute",
            top: 0,
            left: 0,
            width: "100%",
            height: "100%",
            objectFit: "cover",
            transformOrigin: "center 60%",
            ...subject,
          }}
        />
      </AbsoluteFill>

      {(motion.effects ?? []).map((effect) => (
        <Effect
          key={effect}
          kind={effect}
          frame={frame}
          fps={fps}
          width={width}
          height={height}
          seed={seed}
          intensity={intensity}
        />
      ))}

      <Lighting
        kind={motion.lighting ?? "none"}
        seconds={seconds}
        seed={seed}
        progress={progress}
      />
    </AbsoluteFill>
  );
};

function clamp(v: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, v));
}

// ── camera ───────────────────────────────────────────────────────────

function cameraStyle(
  preset: string,
  direction: string,
  progress: number,
  intensity: number,
): React.CSSProperties {
  const eased = easeInOut(progress);

  if (preset === "zoom_focus") {
    // Push in. Starts wide enough that the subject grows into the frame.
    const scale = lerp(eased, 1.06, 1.06 + 0.16 * intensity);
    return { transform: `scale(${scale})` };
  }

  if (preset === "drift") {
    const travel = 2.4 * intensity;
    const rot = 0.45 * intensity;
    const sign = direction === "right" || direction === "down" ? -1 : 1;
    return {
      transform:
        `scale(${1.14 + 0.04 * intensity}) ` +
        `translate(${lerp(eased, sign * travel, -sign * travel)}%, ` +
        `${lerp(eased, -sign * travel * 0.5, sign * travel * 0.5)}%) ` +
        `rotate(${lerp(eased, sign * rot, -sign * rot)}deg)`,
    };
  }

  // parallax — pan across, scaled so an edge never enters the frame.
  const scale = 1.18 + 0.06 * intensity;
  const travel = 4.5 * intensity; // stays under (scale-1)/2 * 100
  let tx = 0;
  let ty = 0;
  if (direction === "left") tx = lerp(eased, travel, -travel);
  else if (direction === "right") tx = lerp(eased, -travel, travel);
  else if (direction === "up") ty = lerp(eased, travel, -travel);
  else ty = lerp(eased, -travel, travel);

  return { transform: `scale(${scale}) translate(${tx}%, ${ty}%)` };
}

// ── subject ──────────────────────────────────────────────────────────

function subjectStyle(
  kind: string,
  seconds: number,
  intensity: number,
): React.CSSProperties {
  // Amplitudes are small on purpose. A still that visibly wobbles reads as a
  // broken render; one that moves a fraction of a percent reads as alive.
  if (kind === "recoil") {
    // A fast settle rather than a loop: the beat has already happened.
    const decay = Math.exp(-seconds * 2.4);
    const shake = Math.sin(seconds * 34) * 0.9 * intensity * decay;
    const pull = 1 + 0.02 * intensity * decay;
    return { transform: `scale(${pull}) translate(${shake}%, ${shake * 0.4}%)` };
  }

  if (kind === "sway") {
    const x = Math.sin(seconds * 1.1) * 0.55 * intensity;
    const r = Math.sin(seconds * 0.9 + 0.6) * 0.28 * intensity;
    return { transform: `translate(${x}%, 0) rotate(${r}deg)` };
  }

  if (kind === "lean") {
    // Weight shifting into the direction of travel.
    const x = Math.sin(seconds * 0.75) * 0.9 * intensity;
    const s = 1 + Math.sin(seconds * 0.6) * 0.006 * intensity;
    return { transform: `translate(${x}%, 0) scale(${s})` };
  }

  // breathe — the default. Two detuned sines so it never reads as a loop.
  const y =
    (Math.sin(seconds * 1.35) * 0.34 + Math.sin(seconds * 0.61) * 0.16) *
    intensity;
  const s = 1 + Math.sin(seconds * 1.35) * 0.0045 * intensity;
  return { transform: `translate(0, ${y}%) scale(${s})` };
}

// ── overlay effects ──────────────────────────────────────────────────

const Effect: React.FC<{
  kind: string;
  frame: number;
  fps: number;
  width: number;
  height: number;
  seed: number;
  intensity: number;
}> = ({ kind, frame, fps, width, height, seed, intensity }) => {
  const seconds = frame / Math.max(fps, 1);

  if (kind === "fog") {
    // Two counter-drifting gradient sheets. Cheaper and more convincing than
    // particles for volume, which is what fog actually is.
    const a = ((seconds * 3.2) % 140) - 20;
    const b = 120 - ((seconds * 2.1) % 140);
    return (
      <AbsoluteFill style={{ pointerEvents: "none", mixBlendMode: "screen" }}>
        <div style={sheet(a, 0.16 * intensity)} />
        <div style={sheet(b, 0.11 * intensity)} />
      </AbsoluteFill>
    );
  }

  const spec = PARTICLES[kind];
  if (!spec) return null;

  const count = Math.round(spec.count * (0.6 + 0.6 * intensity));
  const nodes = [];
  for (let i = 0; i < count; i++) {
    const rx = rand(seed, i * 3 + 1);
    const rs = rand(seed, i * 3 + 2);
    const rd = rand(seed, i * 3 + 3);

    // Each particle runs its own loop, offset so they never arrive together.
    const life = spec.life * (0.7 + rd * 0.6);
    const t = ((seconds + rd * life) % life) / life;

    const size = spec.size[0] + rs * (spec.size[1] - spec.size[0]);
    const drift = Math.sin((seconds + rd * 6) * spec.swayHz) * spec.sway;

    const x = rx * 100 + drift;
    const y = spec.rise ? 100 - t * 118 : t * 118 - 9;
    const fade = spec.fade ? Math.sin(Math.PI * t) : 1;

    nodes.push(
      <div
        key={i}
        style={{
          position: "absolute",
          left: `${x}%`,
          top: `${y}%`,
          width: size,
          height: spec.streak ? size * spec.streak : size,
          borderRadius: spec.streak ? size : "50%",
          background: spec.color,
          opacity: spec.opacity * fade * (0.5 + rs * 0.5),
          filter: spec.blur ? `blur(${spec.blur}px)` : undefined,
          transform: spec.streak ? `rotate(${spec.tilt}deg)` : undefined,
        }}
      />,
    );
  }

  return (
    <AbsoluteFill
      style={{
        pointerEvents: "none",
        mixBlendMode: spec.blend as React.CSSProperties["mixBlendMode"],
      }}
    >
      {nodes}
    </AbsoluteFill>
  );
};

function sheet(x: number, opacity: number): React.CSSProperties {
  return {
    position: "absolute",
    inset: "-10%",
    transform: `translateX(${x}%)`,
    background:
      "radial-gradient(60% 40% at 40% 60%, rgba(220,230,245,.9), transparent 70%)",
    opacity,
    filter: "blur(28px)",
  };
}

type ParticleSpec = {
  count: number;
  life: number;
  size: [number, number];
  color: string;
  opacity: number;
  sway: number;
  swayHz: number;
  rise?: boolean;
  streak?: number;
  tilt: number;
  blur?: number;
  fade?: boolean;
  blend: string;
};

const PARTICLES: Record<string, ParticleSpec> = {
  rain: {
    count: 130, life: 1.1, size: [1.6, 3], color: "rgba(190,215,255,.85)",
    opacity: 0.5, sway: 0.6, swayHz: 0.8, streak: 9, tilt: 8, blend: "screen",
  },
  snow: {
    count: 90, life: 5.5, size: [3, 7], color: "rgba(255,255,255,.95)",
    opacity: 0.75, sway: 3.4, swayHz: 0.5, tilt: 0, blur: 0.6, blend: "screen",
  },
  sparks: {
    count: 34, life: 1.5, size: [2, 4], color: "rgba(255,214,140,1)",
    opacity: 0.95, sway: 2.2, swayHz: 2.4, rise: true, tilt: 0,
    blur: 0.4, fade: true, blend: "screen",
  },
  fire: {
    count: 46, life: 1.9, size: [6, 16], color:
      "radial-gradient(circle, rgba(255,196,92,.95), rgba(255,110,30,.25) 65%, transparent 72%)",
    opacity: 0.7, sway: 2.6, swayHz: 1.7, rise: true, tilt: 0,
    blur: 3, fade: true, blend: "screen",
  },
  smoke: {
    count: 26, life: 5.0, size: [40, 92], color:
      "radial-gradient(circle, rgba(190,190,200,.5), transparent 68%)",
    opacity: 0.32, sway: 4.5, swayHz: 0.42, rise: true, tilt: 0,
    blur: 12, fade: true, blend: "screen",
  },
  dust: {
    count: 70, life: 7.5, size: [1.5, 4], color: "rgba(255,240,210,.9)",
    opacity: 0.4, sway: 5.0, swayHz: 0.33, rise: true, tilt: 0,
    blur: 0.5, fade: true, blend: "screen",
  },
};

// ── lighting ─────────────────────────────────────────────────────────

const Lighting: React.FC<{
  kind: string;
  seconds: number;
  seed: number;
  progress: number;
}> = ({ kind, seconds, seed, progress }) => {
  if (kind === "none") return null;

  if (kind === "flicker") {
    // Irregular on purpose. A sine reads as a pulse, not a failing bulb.
    const step = Math.floor(seconds * 14);
    const n = rand(seed, step);
    const dip = n < 0.14 ? 0.5 : n < 0.26 ? 0.82 : 1;
    return (
      <AbsoluteFill
        style={{
          pointerEvents: "none",
          background: "#000",
          opacity: (1 - dip) * 0.55,
        }}
      />
    );
  }

  if (kind === "sweep") {
    const x = (progress * 150 - 25).toFixed(2);
    return (
      <AbsoluteFill style={{ pointerEvents: "none", mixBlendMode: "screen" }}>
        <div
          style={{
            position: "absolute",
            inset: "-20% -10%",
            transform: `translateX(${x}%) rotate(-12deg)`,
            background:
              "linear-gradient(90deg, transparent, rgba(255,240,200,.30) 45%, transparent 70%)",
            filter: "blur(18px)",
          }}
        />
      </AbsoluteFill>
    );
  }

  // pulse
  const glow = 0.10 + Math.abs(Math.sin(seconds * 1.9)) * 0.16;
  return (
    <AbsoluteFill
      style={{
        pointerEvents: "none",
        mixBlendMode: "screen",
        background:
          "radial-gradient(70% 55% at 50% 55%, rgba(255,90,90,.55), transparent 72%)",
        opacity: glow,
      }}
    />
  );
};
