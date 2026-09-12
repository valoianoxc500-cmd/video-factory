import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { theme } from "../design/theme";

/**
 * A grounded information card for Football News.
 *
 * This is a production fallback, not an emergency placeholder. When no
 * photograph can honestly carry a beat, this is what the viewer sees, so it
 * has to look deliberate: a real editorial graphic with a typographic
 * hierarchy, not a paragraph of text in a grey box.
 *
 * Three rules shape it.
 *
 * It must never read as a photograph. Flat colour, hard geometry, an explicit
 * "verified information" eyebrow, and a visible source line -- the design says
 * "this is a graphic stating facts" at a glance.
 *
 * Every string is supplied already verified. The component renders what it is
 * given and invents nothing: no logos, no crests, no player likenesses, no
 * computed statistics. Anything it displays was checked in Python against the
 * run's citations (see core/verified_cards.py).
 *
 * Layouts vary so a run needing several cards does not show the same panel
 * four times. The variant is chosen deterministically upstream.
 */

export type VerifiedCardLayout = "scoreline" | "fact" | "matchup";

export interface VerifiedCardProps {
  layout?: VerifiedCardLayout;
  /** Small label above the content, e.g. "معلومات موثقة". */
  eyebrow: string;
  /** Team names, for scoreline and matchup layouts. */
  home?: string;
  away?: string;
  home_score?: string;
  away_score?: string;
  /** A single headline fact, for the "fact" layout. */
  headline?: string;
  competition?: string;
  date_text?: string;
  /** "المصدر: espn.com" — always shown; a card without a source is not one. */
  source_line?: string;
  accent_color?: string;
  rtl?: boolean;
}

function shade(hex: string, amount: number): string {
  const clean = hex.startsWith("#") ? hex.slice(1) : hex;
  const n = parseInt(clean.length === 3 ? clean.repeat(2) : clean, 16);
  const r = (n >> 16) & 255;
  const g = (n >> 8) & 255;
  const b = n & 255;
  const mix = (c: number) =>
    Math.max(0, Math.min(255, Math.round(amount < 0 ? c * (1 + amount) : c + (255 - c) * amount)));
  return `rgb(${mix(r)}, ${mix(g)}, ${mix(b)})`;
}

export const VerifiedCard: React.FC<VerifiedCardProps> = ({
  layout = "scoreline",
  eyebrow,
  home = "",
  away = "",
  home_score = "",
  away_score = "",
  headline = "",
  competition = "",
  date_text = "",
  source_line = "",
  accent_color = theme.accent.blue,
  rtl = true,
}) => {
  const frame = useCurrentFrame();
  const { fps, durationInFrames } = useVideoConfig();

  const enter = spring({
    frame,
    fps,
    config: { damping: 26, stiffness: 110 },
    durationInFrames: Math.round(0.7 * fps),
  });
  const fadeOut = interpolate(
    frame,
    [durationInFrames - 0.4 * fps, durationInFrames],
    [1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );
  const rise = interpolate(enter, [0, 1], [28, 0]);
  const opacity = Math.min(enter, 1) * fadeOut;

  const ink = "#FFFFFF";
  const muted = "rgba(255,255,255,0.72)";
  const deep = shade(accent_color, -0.72);
  const lift = shade(accent_color, -0.5);

  // 9:16 safe area. Nothing sits within 8% of an edge, so no line is clipped
  // by a platform's own UI chrome.
  const frameStyle: React.CSSProperties = {
    position: "absolute",
    inset: 0,
    display: "flex",
    flexDirection: "column",
    justifyContent: "center",
    alignItems: "center",
    padding: "8% 9%",
    direction: rtl ? "rtl" : "ltr",
    textAlign: "center",
    fontFamily: theme.font.sans,
  };

  const Eyebrow = (
    <div
      style={{
        fontSize: 30,
        fontWeight: 800,
        letterSpacing: rtl ? 0 : 4,
        color: shade(accent_color, 0.55),
        textTransform: rtl ? "none" : "uppercase",
        marginBottom: 12,
      }}
    >
      {eyebrow}
    </div>
  );

  const Rule = (
    <div
      style={{
        width: 132,
        height: 6,
        borderRadius: 3,
        background: shade(accent_color, 0.4),
        margin: "22px 0",
      }}
    />
  );

  const Meta = (
    <div style={{ marginTop: 26 }}>
      {competition ? (
        <div style={{ fontSize: 40, fontWeight: 700, color: ink, lineHeight: 1.3 }}>
          {competition}
        </div>
      ) : null}
      {date_text ? (
        <div
          style={{
            fontSize: 32,
            fontWeight: 500,
            color: muted,
            marginTop: 8,
            direction: "ltr",
          }}
        >
          {date_text}
        </div>
      ) : null}
    </div>
  );

  const TeamName: React.FC<{ name: string; size?: number }> = ({ name, size = 44 }) => (
    <div
      style={{
        fontSize: size,
        fontWeight: 800,
        color: ink,
        lineHeight: 1.15,
        direction: "ltr",
        maxWidth: 380,
      }}
    >
      {name}
    </div>
  );

  let body: React.ReactNode = null;

  if (layout === "scoreline" && home && away) {
    body = (
      <>
        <div
          style={{
            display: "flex",
            flexDirection: "row",
            alignItems: "center",
            justifyContent: "center",
            gap: 26,
            direction: "ltr",
          }}
        >
          <TeamName name={home} />
          <div
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 14,
              padding: "14px 30px",
              borderRadius: 22,
              background: lift,
              boxShadow: `0 10px 34px rgba(0,0,0,0.35)`,
            }}
          >
            <span style={{ fontSize: 104, fontWeight: 900, color: ink, lineHeight: 1 }}>
              {home_score}
            </span>
            <span style={{ fontSize: 56, fontWeight: 700, color: muted }}>–</span>
            <span style={{ fontSize: 104, fontWeight: 900, color: ink, lineHeight: 1 }}>
              {away_score}
            </span>
          </div>
          <TeamName name={away} />
        </div>
        {Meta}
      </>
    );
  } else if (layout === "matchup" && home && away) {
    body = (
      <>
        <TeamName name={home} size={66} />
        <div
          style={{
            fontSize: 38,
            fontWeight: 800,
            color: shade(accent_color, 0.5),
            margin: "18px 0",
            direction: "ltr",
          }}
        >
          VS
        </div>
        <TeamName name={away} size={66} />
        {Meta}
      </>
    );
  } else {
    body = (
      <>
        <div
          style={{
            fontSize: 54,
            fontWeight: 800,
            color: ink,
            lineHeight: 1.35,
            maxWidth: "100%",
          }}
        >
          {headline || competition || `${home} ${away}`.trim()}
        </div>
        {competition && headline ? Meta : date_text ? Meta : null}
      </>
    );
  }

  return (
    <AbsoluteFill style={{ opacity }}>
      <AbsoluteFill
        style={{
          background: `linear-gradient(165deg, ${lift} 0%, ${deep} 62%, ${shade(
            accent_color,
            -0.85,
          )} 100%)`,
        }}
      />
      {/* A restrained pitch marking: enough to read as football, not enough to
          look like a photograph of a pitch. */}
      <AbsoluteFill style={{ opacity: 0.1 }}>
        <div
          style={{
            position: "absolute",
            left: "50%",
            top: "-14%",
            width: 620,
            height: 620,
            marginLeft: -310,
            borderRadius: "50%",
            border: `10px solid ${shade(accent_color, 0.7)}`,
          }}
        />
        <div
          style={{
            position: "absolute",
            left: "12%",
            right: "12%",
            bottom: "12%",
            height: 260,
            border: `10px solid ${shade(accent_color, 0.7)}`,
            borderBottom: "none",
          }}
        />
      </AbsoluteFill>

      <div style={{ ...frameStyle, transform: `translateY(${rise}px)` }}>
        {Eyebrow}
        {Rule}
        {body}
      </div>

      {source_line ? (
        <div
          style={{
            position: "absolute",
            left: 0,
            right: 0,
            bottom: "6.5%",
            textAlign: "center",
            fontSize: 26,
            fontWeight: 600,
            color: muted,
            direction: rtl ? "rtl" : "ltr",
            fontFamily: theme.font.sans,
            opacity,
          }}
        >
          {source_line}
        </div>
      ) : null}
    </AbsoluteFill>
  );
};
