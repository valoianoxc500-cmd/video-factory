import Link from "next/link";
import { Brand } from "./Brand";

/**
 * Frame for the public legal pages.
 *
 * Deliberately not the AuthShell: these are documents, not a sign-in surface,
 * so they get a single readable column instead of the split marketing layout.
 * Everything else -- palette, type, spacing -- comes from the existing tokens,
 * so nothing about the site's design changes.
 */
export function LegalShell({
  title,
  updated,
  children,
}: {
  title: string;
  updated: string;
  children: React.ReactNode;
}) {
  return (
    <div className="legal-page">
      <header className="legal-head">
        <Link href="/" style={{ textDecoration: "none" }}>
          <Brand legal />
        </Link>
        <nav className="legal-nav">
          <Link href="/privacy-policy">Privacy</Link>
          <Link href="/terms">Terms</Link>
          <Link href="/login">Sign in</Link>
        </nav>
      </header>

      <main className="legal-body">
        <h1>{title}</h1>
        <p className="legal-updated">Last updated {updated}</p>
        {children}
      </main>

      <footer className="legal-foot">
        <Link href="/privacy-policy">Privacy Policy</Link>
        <span aria-hidden>·</span>
        <Link href="/terms">Terms of Service</Link>
      </footer>
    </div>
  );
}
