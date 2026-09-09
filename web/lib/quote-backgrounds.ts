/**
 * Quote Studio backgrounds: a small, fixed, local library.
 *
 * Every background here is drawn from numbers in this file. Nothing is
 * fetched, nothing is generated, and no model is asked for a picture -- which
 * is what makes changing one instant and free, and what makes a project that
 * was saved last month look identical when it is opened today. A generated
 * background could not promise either.
 *
 * Contrast is derived, not typed in. Each background declares the tone the
 * text actually sits on, and the ink, secondary and rule colours are computed
 * from that tone's luminance. Adding a background therefore cannot produce an
 * unreadable slide: there is no field in which to write the wrong ink colour.
 */

export type BackgroundId =
  | "pure_white"
  | "soft_cream"
  | "light_gray"
  | "black"
  | "editorial_paper"
  | "subtle_gradient"
  | "minimal_texture";

/** White is the default, and the one a project falls back to. */
export const DEFAULT_BACKGROUND: BackgroundId = "pure_white";

export type BackgroundSpec = {
  id: BackgroundId;
  label: string;
  hint: string;
  /** The tone the text sits on, as `[r, g, b]`. Contrast is computed from this. */
  base: [number, number, number];
  /** CSS `background` for the live preview. */
  css: string;
};

/**
 * The library, in the order it is offered.
 *
 * Deliberately short and deliberately quiet. A quote card is typography on a
 * surface; a busy surface competes with the words, which is the failure this
 * list is shaped to avoid.
 */
export const BACKGROUNDS: BackgroundSpec[] = [
  {
    id: "pure_white",
    label: "Pure White",
    hint: "The default. Gallery white.",
    base: [255, 255, 255],
    css: "#FFFFFF",
  },
  {
    id: "soft_cream",
    label: "Soft Cream",
    hint: "Warm paper white.",
    base: [250, 246, 238],
    css: "#FAF6EE",
  },
  {
    id: "light_gray",
    label: "Light Gray",
    hint: "Cool neutral.",
    base: [241, 241, 243],
    css: "#F1F1F3",
  },
  {
    id: "black",
    label: "Black",
    hint: "Full inversion.",
    base: [11, 11, 12],
    css: "#0B0B0C",
  },
  {
    id: "editorial_paper",
    label: "Editorial Paper",
    hint: "Off-white with a fine fibre.",
    base: [247, 245, 240],
    css:
      "radial-gradient(120% 90% at 50% 0%, #FBFAF7 0%, #F4F1EA 100%)",
  },
  {
    id: "subtle_gradient",
    label: "Subtle Gradient",
    hint: "A slow fall from white.",
    base: [246, 247, 249],
    css: "linear-gradient(170deg, #FFFFFF 0%, #F4F5F8 58%, #EBEDF2 100%)",
  },
  {
    id: "minimal_texture",
    label: "Minimal Texture",
    hint: "Barely-there grain.",
    base: [249, 249, 250],
    css:
      "repeating-linear-gradient(45deg, #FAFAFB 0 2px, #F6F6F8 2px 4px)",
  },
];

export function backgroundById(id: string): BackgroundSpec {
  return (
    BACKGROUNDS.find((b) => b.id === id) ??
    BACKGROUNDS.find((b) => b.id === DEFAULT_BACKGROUND)!
  );
}

export function isBackgroundId(value: unknown): value is BackgroundId {
  return BACKGROUNDS.some((b) => b.id === value);
}

/**
 * A stored value turned into a usable one.
 *
 * Projects saved before backgrounds existed have no stored id, and a project
 * saved against a background later removed from the library has one that no
 * longer resolves. Both land on white rather than on an error.
 */
export function normaliseBackground(value: unknown): BackgroundId {
  return isBackgroundId(value) ? value : DEFAULT_BACKGROUND;
}

// ── contrast ─────────────────────────────────────────────────────────

/** WCAG relative luminance. */
export function luminance([r, g, b]: [number, number, number]): number {
  const channel = (v: number) => {
    const s = v / 255;
    return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b);
}

/** WCAG contrast ratio between two tones, 1..21. */
export function contrastRatio(
  a: [number, number, number],
  b: [number, number, number],
): number {
  const la = luminance(a);
  const lb = luminance(b);
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

export type BackgroundInk = {
  /** The quote itself. */
  ink: string;
  /** Name, handle, and anything supporting. */
  secondary: string;
  /** Hairlines and dividers. */
  rule: string;
  /** The quote mark and the one accented detail. */
  accent: string;
  /** Ring around the profile photo, so it never dissolves into the surface. */
  photoRing: string;
  /** True when the surface is dark and the ink is light. */
  isDark: boolean;
};

const NEAR_BLACK: [number, number, number] = [17, 17, 19];
const NEAR_WHITE: [number, number, number] = [250, 250, 252];

/**
 * The readable palette for a background.
 *
 * The ink is whichever of near-black and near-white contrasts more with the
 * surface, so a background added later cannot ship unreadable text. Secondary
 * and rule tones are the same hue at reduced alpha rather than a lighter grey:
 * alpha keeps its relationship to whatever is behind it, which matters on the
 * gradient and texture surfaces where the tone is not uniform.
 */
export function inkFor(background: BackgroundSpec): BackgroundInk {
  const onDark = contrastRatio(background.base, NEAR_WHITE);
  const onLight = contrastRatio(background.base, NEAR_BLACK);
  const isDark = onDark > onLight;

  const [r, g, b] = isDark ? NEAR_WHITE : NEAR_BLACK;
  const rgba = (alpha: number) => `rgba(${r}, ${g}, ${b}, ${alpha})`;

  return {
    ink: `rgb(${r}, ${g}, ${b})`,
    secondary: rgba(0.62),
    rule: rgba(0.14),
    // A single restrained accent. Warm on light surfaces, warmer still on
    // dark ones so it does not read as dirty grey.
    accent: isDark ? "#C9A96A" : "#8A6A2F",
    photoRing: rgba(0.18),
    isDark,
  };
}

/** Whether a background's computed ink clears WCAG AA for large text (3:1). */
export function meetsContrast(background: BackgroundSpec): boolean {
  const { isDark } = inkFor(background);
  return (
    contrastRatio(background.base, isDark ? NEAR_WHITE : NEAR_BLACK) >= 3
  );
}

// ── canvas painting ──────────────────────────────────────────────────

/** Deterministic 0..1 from an integer. Export must be byte-stable. */
function rand(n: number): number {
  let x = (n * 374761393 + 668265263) & 0xffffffff;
  x = (x ^ (x >>> 13)) * 1274126177;
  x = x ^ (x >>> 16);
  return ((x >>> 0) % 100000) / 100000;
}

/**
 * Paint a background onto a canvas for export.
 *
 * Mirrors the CSS in `css` above. Canvas cannot read a CSS gradient string,
 * so the stops are restated here; the two are kept in the same order and the
 * same colours so the exported PNG matches what the preview showed.
 */
export function paintBackground(
  ctx: CanvasRenderingContext2D,
  id: BackgroundId,
  width: number,
  height: number,
): void {
  const spec = backgroundById(id);

  switch (spec.id) {
    case "editorial_paper": {
      const grad = ctx.createRadialGradient(
        width / 2, 0, 0,
        width / 2, 0, Math.max(width, height) * 1.1,
      );
      grad.addColorStop(0, "#FBFAF7");
      grad.addColorStop(1, "#F4F1EA");
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, width, height);
      paintFibre(ctx, width, height);
      return;
    }
    case "subtle_gradient": {
      // 170deg in CSS ≈ top-left to bottom-right, mostly vertical.
      const grad = ctx.createLinearGradient(width * 0.12, 0, width * -0.12, height);
      grad.addColorStop(0, "#FFFFFF");
      grad.addColorStop(0.58, "#F4F5F8");
      grad.addColorStop(1, "#EBEDF2");
      ctx.fillStyle = grad;
      ctx.fillRect(0, 0, width, height);
      return;
    }
    case "minimal_texture": {
      ctx.fillStyle = "#FAFAFB";
      ctx.fillRect(0, 0, width, height);
      paintGrain(ctx, width, height);
      return;
    }
    default: {
      const [r, g, b] = spec.base;
      ctx.fillStyle = `rgb(${r}, ${g}, ${b})`;
      ctx.fillRect(0, 0, width, height);
    }
  }
}

/** Sparse short strokes, like paper fibre. Never strong enough to read as noise. */
function paintFibre(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
): void {
  const count = Math.round((width * height) / 26000);
  ctx.save();
  ctx.lineWidth = 1;
  for (let i = 0; i < count; i++) {
    const x = rand(i * 3 + 1) * width;
    const y = rand(i * 3 + 2) * height;
    const len = 4 + rand(i * 3 + 3) * 12;
    const dark = rand(i * 7 + 5) > 0.5;
    ctx.strokeStyle = dark
      ? "rgba(120, 108, 88, 0.05)"
      : "rgba(255, 255, 255, 0.5)";
    ctx.beginPath();
    ctx.moveTo(x, y);
    ctx.lineTo(x + len, y + (rand(i * 3 + 4) - 0.5) * 2);
    ctx.stroke();
  }
  ctx.restore();
}

/** The 45° hairline weave from the CSS, drawn at export resolution. */
function paintGrain(
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
): void {
  ctx.save();
  ctx.strokeStyle = "#F6F6F8";
  ctx.lineWidth = 2;
  const step = 4;
  for (let d = -height; d < width + height; d += step * 2) {
    ctx.beginPath();
    ctx.moveTo(d, 0);
    ctx.lineTo(d + height, height);
    ctx.stroke();
  }
  ctx.restore();
}
