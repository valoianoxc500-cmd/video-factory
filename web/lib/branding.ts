/**
 * Every piece of brand identity, in one place.
 *
 * Naming, logo, tagline and palette are deliberately placeholders. Change them
 * here and the whole application follows -- nothing else hardcodes the app
 * name, and no component draws its own mark. Renaming later is an edit to this
 * file, not a redesign.
 */

export const branding = {
  /** Shown in the sidebar, page titles and auth screens. */
  appName: "FirstVideoCheck",
  /** Set in the logo lockup: the second word takes the accent colour. */
  nameSplit: ["First", "VideoCheck"] as const,
  /** Small caps line under the name. */
  tagline: "Real stories. Verified. In seconds.",
  /** Longer line for the auth screens' left panel. */
  pitch:
    "AI-powered video creation with verified sources, stunning visuals " +
    "and professional narration.",
  /**
   * The name used where a real, legally meaningful identifier is required --
   * the Privacy Policy and Terms. `appName` above is a deliberate placeholder,
   * and legal text cannot say "YOUR APP NAME". This matches the application
   * title registered on the Google OAuth consent screen.
   */
  legalName: "Video Factory",
  /** Where privacy and terms enquiries go. The OAuth consent screen's support address. */
  contactEmail: "valoianoxc500@gmail.com",
  /** Emoji favicon until a real mark exists; swap for /favicon.ico when ready. */
  favicon: "🎬",
  /**
   * Accent used for focus rings, primary buttons and active nav. Channel
   * themes override this locally; this is the product's own colour.
   */
  accent: "#14E08C",
} as const;

export type Branding = typeof branding;
