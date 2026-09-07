import { branding } from "@/lib/branding";

/**
 * The application's mark and name.
 *
 * A play triangle cut out of a rounded square: the product turns a claim into
 * something you press play on, and the notch on the right reads as the second
 * half of a check. Drawn inline so there is no asset to load and no layout
 * shift while it arrives.
 *
 * Everything it renders comes from lib/branding.ts -- renaming the product is
 * an edit to that file.
 */
export function Brand({
  showTagline = true,
  /**
   * Render the legal entity name instead of the display name. A legal
   * document must not be headed by a marketing lockup, so the Privacy Policy
   * and Terms opt into `legalName`.
   */
  legal = false,
  /** The product-class badge beside the name, as on the sign-in screen. */
  badge,
}: {
  showTagline?: boolean;
  legal?: boolean;
  badge?: string;
}) {
  const [first, second] = branding.nameSplit;

  return (
    <div className="brand">
      <span className="brand-mark" aria-hidden>
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none">
          <path
            d="M9 7.2v9.6a.8.8 0 0 0 1.22.68l7.5-4.8a.8.8 0 0 0 0-1.36l-7.5-4.8A.8.8 0 0 0 9 7.2Z"
            fill="currentColor"
          />
        </svg>
      </span>
      <span>
        <span className="brand-name">
          {legal ? (
            branding.legalName
          ) : (
            <>
              {first}
              <span className="brand-accent">{second}</span>
            </>
          )}
          {badge && <span className="brand-badge">{badge}</span>}
        </span>
        {showTagline && <span className="brand-tag">{branding.tagline}</span>}
      </span>
    </div>
  );
}
