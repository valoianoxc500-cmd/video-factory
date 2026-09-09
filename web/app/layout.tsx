import type { Metadata } from "next";
import { branding } from "@/lib/branding";
import "./globals.css";
// The whole design system lives here -- auth pages and dashboard alike. It was
// imported only by the dashboard layout, which left login, signup and the two
// password pages rendering as unstyled HTML.
import "./auth-styles.css";
// Loaded last: it retunes the tokens above and restyles the shells without
// renaming a single class, so components keep working untouched.
import "./redesign.css";
// The cinematic system. Same technique again, one layer further on: it
// retunes redesign.css rather than replacing it, so nothing that already
// works can break on a class it happens not to cover.
import "./premium.css";

export const metadata: Metadata = {
  title: `${branding.appName} — ${branding.tagline}`,
  description: branding.pitch,
  icons: {
    icon: `data:image/svg+xml,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><text y=".9em" font-size="90">${branding.favicon}</text></svg>`,
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <link rel="preconnect" href="https://fonts.googleapis.com" />
        <link
          rel="preconnect"
          href="https://fonts.gstatic.com"
          crossOrigin="anonymous"
        />
        {/* Jakarta for display weight, Inter for reading, Caveat for the one
            handwritten aside on the create screen. */}
        <link
          href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@500;600;700;800&family=Inter:wght@400;500;600&family=Caveat:wght@600&display=swap"
          rel="stylesheet"
        />
        {/* Quote Studio's typography choices. Loaded here rather than in the
            page because a slide is exported through canvas, and canvas can
            only draw a face the document has already loaded -- a font
            requested at export time renders as the fallback. Arabic faces
            are listed first: they are the ones with no usable default. */}
        <link
          href="https://fonts.googleapis.com/css2?family=Noto+Naskh+Arabic:wght@400;700&family=Noto+Kufi+Arabic:wght@400;700&family=Cairo:wght@600;800&family=Playfair+Display:wght@700;900&family=JetBrains+Mono:wght@600&display=swap"
          rel="stylesheet"
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
