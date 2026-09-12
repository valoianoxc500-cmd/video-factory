import React from "react";
import {
  AbsoluteFill,
  Img,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
  interpolate,
  spring,
} from "remotion";
import { theme } from "../design/theme";
import { renderHighlightedText } from "../lib/highlightWords";
import { StarfieldBackground } from "./StarfieldBackground";

// ── Types ───────────────────────────────────────────────────

export interface InfoCardProps {
  text: string;
  background_color?: string;
  text_box_color?: string;
  illustration_url?: string;
  illustration_style?: "raw" | "framed";
  frame_border_color?: string;
  frame_border_width?: number;
  layout?: "image-left" | "image-right";
  show_particles?: boolean;
  accent_color?: string;
  highlighted_keywords?: string[];
  highlight_color?: string;
}

// ── Helpers ─────────────────────────────────────────────────

/** Lighten a hex color by mixing toward white. */
function lighten(hex: string, amount: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  const lr = Math.round(r + (255 - r) * amount);
  const lg = Math.round(g + (255 - g) * amount);
  const lb = Math.round(b + (255 - b) * amount);
  return `#${lr.toString(16).padStart(2, "0")}${lg.toString(16).padStart(2, "0")}${lb.toString(16).padStart(2, "0")}`;
}

/** Darken a hex color by mixing toward black. */
function darken(hex: string, amount: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  const dr = Math.round(r * (1 - amount));
  const dg = Math.round(g * (1 - amount));
  const db = Math.round(b * (1 - amount));
  return `#${dr.toString(16).padStart(2, "0")}${dg.toString(16).padStart(2, "0")}${db.toString(16).padStart(2, "0")}`;
}

// ── Component ───────────────────────────────────────────────

export const InfoCard: React.FC<InfoCardProps> = ({
  text,
  background_color,
  text_box_color,
  illustration_url,
  illustration_style = "raw",
  frame_border_color,
  frame_border_width = 3,
  layout = "image-left",
  show_particles = true,
  accent_color = theme.accent.blue,
  highlighted_keywords = [],
  highlight_color = "#FFD600",
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  const bgColor = background_color || darken(accent_color, 0.7);
  const boxColor = text_box_color || lighten(accent_color, 0.15);

  // Global fade in/out
  const fadeIn = interpolate(frame, [0, 0.4 * fps], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
  const fadeOut = interpolate(
    frame,
    [durationInFrames - 0.5 * fps, durationInFrames],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );
  const opacity = fadeIn * fadeOut;

  // Illustration entrance
  const illustrationSpring = spring({
    frame,
    fps,
    config: { damping: 20, stiffness: 120 },
    durationInFrames: Math.round(0.83 * fps),
  });
  const illustrationScale = interpolate(illustrationSpring, [0, 1], [0.6, 1]);
  const illustrationOpacity = interpolate(illustrationSpring, [0, 0.5], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  // Text box entrance (staggered)
  const textSpring = spring({
    frame,
    fps,
    delay: Math.round(0.27 * fps),
    config: { damping: 22, stiffness: 100 },
    durationInFrames: Math.round(1 * fps),
  });
  const textSlideX = interpolate(
    textSpring,
    [0, 1],
    [layout === "image-left" ? 60 : -60, 0],
  );
  const textOpacity = interpolate(textSpring, [0, 0.4], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });

  const isImageLeft = layout === "image-left";
  const isFramed = illustration_style === "framed";
  const borderColor = frame_border_color || lighten(accent_color, 0.3);

  const illustrationSide = (
    <div
      style={{
        width: isFramed ? "45%" : "40%",
        height: "100%",
        display: "flex",
        justifyContent: "center",
        alignItems: "center",
        opacity: illustrationOpacity,
        transform: `scale(${illustrationScale})`,
      }}
    >
      {illustration_url ? (
        <Img
          src={
            illustration_url.startsWith("http")
              ? illustration_url
              : staticFile(illustration_url)
          }
          style={{
            maxWidth: isFramed ? "85%" : "70%",
            maxHeight: isFramed ? "75%" : "60%",
            objectFit: "cover",
            borderRadius: isFramed ? 12 : 16,
            ...(isFramed
              ? {
                  border: `${frame_border_width}px solid ${borderColor}`,
                  boxShadow: `0 8px 32px rgba(0,0,0,0.4), 0 0 0 1px ${borderColor}20`,
                }
              : {
                  filter: "drop-shadow(0 8px 24px rgba(0,0,0,0.4))",
                }),
          }}
        />
      ) : (
        <div
          style={{
            width: 200,
            height: 200,
            borderRadius: "50%",
            backgroundColor: accent_color,
            opacity: 0.3,
            boxShadow: `0 0 60px ${accent_color}40`,
          }}
        />
      )}
    </div>
  );

  const textSide = (
    <div
      style={{
        width: "55%",
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "0 40px",
        opacity: textOpacity,
        transform: `translateX(${textSlideX}px)`,
      }}
    >
      <div
        style={{
          backgroundColor: boxColor,
          borderRadius: 24,
          padding: "48px 44px",
          maxWidth: "100%",
          boxShadow: `0 8px 32px rgba(0,0,0,0.3), inset 0 1px 0 ${lighten(boxColor, 0.2)}40`,
        }}
      >
        <div
          style={{
            color: "#FFFFFF",
            fontSize: 36,
            fontWeight: 700,
            fontFamily: theme.font.sans,
            lineHeight: 1.4,
            textShadow: "0 1px 4px rgba(0,0,0,0.3)",
            // Line breaks in props.text are meaningful. A verified information
            // card is a stack of separate facts -- heading, result,
            // competition, date, source -- and collapsing them into one
            // paragraph is how a scoreline ends up reading as prose.
            whiteSpace: "pre-line",
          }}
        >
          {renderHighlightedText(text, highlighted_keywords, highlight_color)}
        </div>
      </div>
    </div>
  );

  return (
    <AbsoluteFill style={{ opacity }}>
      {/* Background */}
      {show_particles ? (
        <StarfieldBackground
          particle_count={25}
          drift_speed={0.8}
          gradient_start={bgColor}
          gradient_end={lighten(bgColor, 0.15)}
          seed={text.length}
        />
      ) : (
        <AbsoluteFill
          style={{
            background: `radial-gradient(ellipse 80% 70% at 50% 40%, ${lighten(bgColor, 0.15)} 0%, ${bgColor} 100%)`,
          }}
        />
      )}

      {/* Content layout */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "row",
          alignItems: "center",
          padding: "0 5%",
        }}
      >
        {isImageLeft ? (
          <>
            {illustrationSide}
            {textSide}
          </>
        ) : (
          <>
            {textSide}
            {illustrationSide}
          </>
        )}
      </div>
    </AbsoluteFill>
  );
};
