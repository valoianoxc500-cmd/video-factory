import Link from "next/link";
import { Brand } from "./Brand";
import { branding } from "@/lib/branding";

/**
 * Shared frame for sign-in, sign-up and password recovery.
 *
 * The left panel is the pitch, the right is the one thing to do. The collage
 * stands in for the videos this product makes -- it is drawn from gradients
 * rather than stock photography, so it never waits on a request, never ships
 * someone else's footage, and cannot go stale.
 */

const POINTS = [
  ["Research", "Trusted sources", "M21 21l-4.3-4.3M11 19a8 8 0 1 1 0-16 8 8 0 0 1 0 16Z"],
  ["Write", "Engaging scripts", "M5 4h9l5 5v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm9 0v5h5M8 13h8M8 17h5"],
  ["Generate", "Stunning visuals", "M4 5h16v14H4z M4 15l4.5-4.5L13 15l3-3 4 4"],
  ["Narrate", "Realistic voice", "M6 10v4m4-7v10m4-13v16m4-11v6"],
] as const;

/** Each tile is a still from a different kind of story this tool tells. */
const TILES = [
  ["tile-sm", "linear-gradient(150deg,#1D2A38,#0A1016)"],
  ["tile-tall", "linear-gradient(150deg,#243347,#0C1219)"],
  ["tile-lead", "linear-gradient(155deg,#33506B 0%,#16222E 55%,#0A1016 100%)"],
  ["tile-sm", "linear-gradient(150deg,#3A2440,#120C16)"],
  ["tile-wide", "linear-gradient(150deg,#1E3A4C,#0A1016)"],
  ["tile-sm", "linear-gradient(150deg,#402A22,#160E0C)"],
  ["tile-tall", "linear-gradient(150deg,#22384A,#0B1117)"],
] as const;

export function AuthShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="auth-shell">
      <aside className="auth-aside">
        <Link href="/" style={{ textDecoration: "none" }}>
          <Brand badge="SaaS" />
        </Link>

        <div className="auth-reel" aria-hidden>
          {TILES.map(([shape, tile], i) => (
            <i
              key={i}
              className={shape}
              style={{ ["--tile" as string]: tile }}
            />
          ))}
        </div>

        <div className="auth-pitch">
          <h2>
            Turn any topic
            <br />
            into a <span className="hl">viral video</span>
          </h2>
          <p>{branding.pitch}</p>

          <ul className="auth-points">
            {POINTS.map(([title, sub, d]) => (
              <li key={title}>
                <span className="pt-icon" aria-hidden>
                  <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
                    <path
                      d={d}
                      stroke="currentColor"
                      strokeWidth="1.8"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </span>
                <span>
                  <strong>{title}</strong>
                  <small>{sub}</small>
                </span>
              </li>
            ))}
          </ul>
        </div>
      </aside>

      <main className="auth-main">
        <div className="auth-card">
          {children}
          <p className="auth-legal">
            <Link href="/privacy-policy">Privacy Policy</Link>
            <span aria-hidden> · </span>
            <Link href="/terms">Terms of Service</Link>
          </p>
        </div>
      </main>
    </div>
  );
}
