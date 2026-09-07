export const theme = {
  bg: "#0A0E1A",
  surface: "#141B2D",
  gridLine: "#1E2A42",
  text: {
    primary: "#E8ECF4",
    secondary: "#8B95A8",
    muted: "#4A5568",
  },
  accent: {
    blue: "#00D4FF",
    green: "#00E676",
    red: "#FF4444",
    amber: "#FFB300",
  },
  font: {
    sans: "Inter, Arial, sans-serif",
    mono: "'JetBrains Mono', 'Courier New', monospace",
    // Faces that actually ship Arabic glyphs, in Windows/macOS/Linux order.
    // Inter has none, so Arabic text must not fall back to the sans stack.
    arabic:
      "'Segoe UI', 'Noto Naskh Arabic', 'Noto Sans Arabic', Tahoma, " +
      "'Geeza Pro', 'Arabic Typesetting', Arial, sans-serif",
  },
  scrim: {
    center: "rgba(0,0,0,0.55)",
    mid: "rgba(0,0,0,0.35)",
    edge: "rgba(0,0,0,0)",
  },
  spacing: {
    chartPadding: { top: 120, right: 80, bottom: 80, left: 100 },
  },
} as const;
