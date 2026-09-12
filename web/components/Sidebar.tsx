"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Brand } from "./Brand";
import { createClient } from "@/lib/supabase/client";

const Icon = {
  overview: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm9 0h7v-9h-7v9Zm0-16v5h7V4h-7Z",
  football:
    "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Zm0 4.2 3.9 2.8-1.5 4.6H9.6L8.1 10 12 7.2ZM12 3v4.2M4.2 9.5 8.1 10m-1.4 8L9.6 14.6m7.7 3.4-2.9-3.4M19.8 9.5 15.9 10",
  story:
    "M5 4h9l5 5v11a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Zm9 0v5h5M8 13h8M8 17h5",
  horror:
    "M12 2c4.4 0 8 3.4 8 7.6 0 2.7-1 4.3-2 5.4v3.5a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 6 18.5V15c-1-1.1-2-2.7-2-5.4C4 5.4 7.6 2 12 2Zm-2.4 7.8a1.3 1.3 0 1 0 0 2.6 1.3 1.3 0 0 0 0-2.6Zm4.8 0a1.3 1.3 0 1 0 0 2.6 1.3 1.3 0 0 0 0-2.6Z",
  truth:
    "M12 2 4 5.5v6c0 4.6 3.2 8.8 8 10.5 4.8-1.7 8-5.9 8-10.5v-6L12 2Zm-1 12.5-3-3 1.4-1.4L11 11.6l4.6-4.6L17 8.4l-6 6.1Z",
  clipping:
    "M6 3v10m12-10v10M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm12 0a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM8.1 15.9 18 3M15.9 15.9 6 3",
  analytics: "M4 20V10m6 10V4m6 16v-7",
  library: "M4 6h16M4 12h16M4 18h10",
  settings:
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.1l2-1.6-2-3.4-2.4 1a7.5 7.5 0 0 0-1.9-1.1L14.6 3H9.4l-.4 2.8c-.7.3-1.3.6-1.9 1.1l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.2l-2 1.6 2 3.4 2.4-1c.6.5 1.2.8 1.9 1.1l.4 2.8h5.2l.4-2.8c.7-.3 1.3-.6 1.9-1.1l2.4 1 2-3.4-2-1.6c.1-.4.1-.7.1-1.1Z",
  analyzer:
    "M11 4a7 7 0 1 0 0 14 7 7 0 0 0 0-14Zm10 17-5.2-5.2M8.5 11h5M11 8.5v5",
  animated:
    "M12 3a2.2 2.2 0 1 0 0 4.4A2.2 2.2 0 0 0 12 3Zm0 4.4v6.2m0 0-3 6.4m3-6.4 3 6.4M7 10.2l5 1.4 5-1.4",
  credits:
    "M3 8h18v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8Zm0 0 2-3h14l2 3M8 13h4",
  quotes:
    "M9 7H6a2 2 0 0 0-2 2v3a2 2 0 0 0 2 2h1a3 3 0 0 1-3 3m14-10h-3a2 2 0 0 0-2 2v3a2 2 0 0 0 2 2h1a3 3 0 0 1-3 3",
  chat:
    "M4 5.5A2.5 2.5 0 0 1 6.5 3h11A2.5 2.5 0 0 1 20 5.5v8a2.5 2.5 0 0 1-2.5 2.5H10l-5 4v-4.5A2.5 2.5 0 0 1 4 13.5v-8ZM8 8h8M8 12h5",
} as const;

function NavIcon({ d }: { d: string }) {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
      <path d={d} stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

export function Sidebar({ email }: { email: string }) {
  const pathname = usePathname();
  const router = useRouter();

  const is = (href: string) =>
    href === "/dashboard" ? pathname === href : pathname.startsWith(href);

  async function signOut() {
    await createClient().auth.signOut();
    router.push("/login");
    router.refresh();
  }

  const storyOpen = is("/dashboard/story") || is("/dashboard/animated");

  return (
    <aside className="side">
      <Link href="/dashboard" style={{ textDecoration: "none" }}>
        <Brand />
      </Link>

      <nav className="nav">
        <span className="nav-label">Workspace</span>
        <Link href="/dashboard" className={is("/dashboard") ? "active" : ""}>
          <NavIcon d={Icon.overview} />
          <span>
            Dashboard
            <span className="nav-sub">Everything in your workspace</span>
          </span>
        </Link>

        <span className="nav-label">Create</span>
        <Link
          href="/dashboard/football"
          data-surface="football"
          className={is("/dashboard/football") ? "active" : ""}
        >
          <NavIcon d={Icon.football} />
          <span>
            Football
            <span className="nav-sub">Researched Arabic football news</span>
          </span>
        </Link>

        <span className={`nav-parent${storyOpen ? " is-open" : ""}`}>
          <NavIcon d={Icon.story} /> Story To Video
        </span>
        <span className="nav-children">
          <Link
            href="/dashboard/story/horror"
            data-surface="horror"
            className={is("/dashboard/story/horror") ? "active" : ""}
          >
            <NavIcon d={Icon.horror} />
            <span>
              Horror Stories
              <span className="nav-sub">Paranormal, legends, original horror</span>
            </span>
          </Link>
          <Link
            href="/dashboard/story/true"
            data-surface="true"
            className={is("/dashboard/story/true") ? "active" : ""}
          >
            <NavIcon d={Icon.truth} />
            <span>
              True Stories
              <span className="nav-sub">Real cases, verified before written</span>
            </span>
          </Link>
          <Link
            href="/dashboard/animated"
            data-surface="animated"
            className={is("/dashboard/animated") ? "active" : ""}
          >
            <NavIcon d={Icon.animated} />
            <span>
              Animated Stories
              <span className="nav-sub">One visual world, scene to scene</span>
            </span>
          </Link>
        </span>

        <span className="nav-label">Tools</span>
        <Link
          href="/dashboard/quotes"
          data-surface="quotes"
          className={is("/dashboard/quotes") ? "active" : ""}
        >
          <NavIcon d={Icon.quotes} />
          <span>
            Quote Studio
            <span className="nav-sub">Photo to quote carousel</span>
          </span>
        </Link>

        <Link
          href="/dashboard/fake-chat"
          data-surface="fake-chat"
          className={is("/dashboard/fake-chat") ? "active" : ""}
        >
          <NavIcon d={Icon.chat} />
          <span>
            Fake Chat Studio
            <span className="nav-sub">Design realistic chat mockups</span>
          </span>
        </Link>

        <Link
          href="/dashboard/clipping"
          data-surface="clipping"
          className={is("/dashboard/clipping") ? "active" : ""}
        >
          <NavIcon d={Icon.clipping} />
          <span>
            Clipping
            <span className="nav-sub">Your footage, cut to 9:16</span>
          </span>
        </Link>

        <Link
          href="/dashboard/analyzer"
          data-surface="analyzer"
          className={is("/dashboard/analyzer") ? "active" : ""}
        >
          <NavIcon d={Icon.analyzer} />
          <span>
            Clip Analyzer
            <span className="nav-sub">Why a video performed, step by step</span>
          </span>
        </Link>

        <span className="nav-label">Library</span>
        <Link
          href="/dashboard/reels/analytics"
          data-surface="analytics"
          className={is("/dashboard/reels/analytics") ? "active" : ""}
        >
          <NavIcon d={Icon.analytics} /> Analytics
        </Link>
        <Link href="/dashboard/library" data-surface="library" className={is("/dashboard/library") ? "active" : ""}>
          <NavIcon d={Icon.library} /> Library
        </Link>

        <span className="nav-label">Account</span>
        <Link href="/dashboard/settings" className={is("/dashboard/settings") ? "active" : ""}>
          <NavIcon d={Icon.settings} /> Settings
        </Link>
        <Link href="/dashboard/plans" className={is("/dashboard/plans") ? "active" : ""}>
          <NavIcon d={Icon.credits} /> Credits
        </Link>
      </nav>

      <div className="side-foot">
        <div className="side-user-card">
          <span className="side-avatar" aria-hidden>
            {(email.trim()[0] ?? "?").toUpperCase()}
          </span>
          <span style={{ minWidth: 0 }}>
            <p className="side-user" title={email}>
              {email.split("@")[0]}
            </p>
            <p className="side-plan">{email}</p>
          </span>
        </div>
        <button
          className="btn-ghost"
          type="button"
          onClick={signOut}
          style={{ marginTop: 10 }}
        >
          Sign out
        </button>
        <p className="side-legal">
          <Link href="/privacy-policy">Privacy</Link>
          <span aria-hidden> · </span>
          <Link href="/terms">Terms</Link>
        </p>
      </div>
    </aside>
  );
}
