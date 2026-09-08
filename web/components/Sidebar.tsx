"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Brand } from "./Brand";
import { createClient } from "@/lib/supabase/client";

const Icon = {
  overview: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm9 0h7v-9h-7v9Zm0-16v5h7V4h-7Z",
  create: "M12 5v14M5 12h14",
  library: "M4 6h16M4 12h16M4 18h10",
  jobs: "M12 8v4l3 2m6-2a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z",
  settings:
    "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.1l2-1.6-2-3.4-2.4 1a7.5 7.5 0 0 0-1.9-1.1L14.6 3H9.4l-.4 2.8c-.7.3-1.3.6-1.9 1.1l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.2l-2 1.6 2 3.4 2.4-1c.6.5 1.2.8 1.9 1.1l.4 2.8h5.2l.4-2.8c.7-.3 1.3-.6 1.9-1.1l2.4 1 2-3.4-2-1.6c.1-.4.1-.7.1-1.1Z",
  discover: "M21 21l-4.3-4.3M11 19a8 8 0 1 1 0-16 8 8 0 0 1 0 16Z",
  recreate:
    "M4 6h11a4 4 0 0 1 0 8H7m0 0 3-3m-3 3 3 3M4 4v4h4",
  credits:
    "M3 8h18v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V8Zm0 0 2-3h14l2 3M8 13h4",
  clipping:
    "M6 3v10m12-10v10M6 21a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm12 0a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM8.1 15.9 18 3M15.9 15.9 6 3",
  saved: "M6 4h12v16l-6-4-6 4V4Z",
  queue: "M4 6h16M4 12h16M4 18h16M2 6h.01M2 12h.01M2 18h.01",
  scheduled: "M8 3v4m8-4v4M4 9h16M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1Z",
  published: "M5 13l4 4L19 7",
  accounts: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm13 10v-2a4 4 0 0 0-3-3.9",
  analytics: "M4 20V10m6 10V4m6 16v-7",
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

  return (
    <aside className="side">
      <Link href="/dashboard" style={{ textDecoration: "none" }}>
        <Brand />
      </Link>

      {/*
        Nine destinations under three headings -- the things you actually come
        here to do, named the way you would ask for them.

        Deliberately still short. The publishing states (saved, queue,
        scheduled, published) and connected accounts are not here: they are
        stages of work you are already looking at, so they live inside the
        screen that owns them rather than competing with it in the nav.
      */}
      <nav className="nav">
        <span className="nav-label">Make</span>
        <Link href="/dashboard" className={is("/dashboard") ? "active" : ""}>
          <NavIcon d={Icon.overview} /> Dashboard
        </Link>
        <Link href="/dashboard/create" className={is("/dashboard/create") ? "active" : ""}>
          <NavIcon d={Icon.create} />
          <span>
            Create Video
            <span className="nav-sub">A finished video from a topic</span>
          </span>
        </Link>
        <Link
          href="/dashboard/clipping"
          className={is("/dashboard/clipping") ? "active" : ""}
        >
          <NavIcon d={Icon.clipping} />
          <span>
            Clipping
            <span className="nav-sub">Your footage, cut to 9:16</span>
          </span>
        </Link>

        <span className="nav-label">Grow</span>
        {/* Discover lives at /dashboard/reels. The nav used to call this
            "Re-Create", which is a different screen entirely -- the label and
            the page it opened disagreed. */}
        <Link
          href="/dashboard/reels"
          className={pathname === "/dashboard/reels" ? "active" : ""}
        >
          <NavIcon d={Icon.discover} />
          <span>
            Find Viral
            <span className="nav-sub">What is performing right now</span>
          </span>
        </Link>
        <Link
          href="/dashboard/reels/videos"
          className={is("/dashboard/reels/videos") ? "active" : ""}
        >
          <NavIcon d={Icon.recreate} />
          <span>
            Re-Create
            <span className="nav-sub">Turn a video you own into a new one</span>
          </span>
        </Link>
        <Link href="/dashboard/library" className={is("/dashboard/library") ? "active" : ""}>
          <NavIcon d={Icon.library} /> Library
        </Link>
        <Link
          href="/dashboard/reels/analytics"
          className={is("/dashboard/reels/analytics") ? "active" : ""}
        >
          <NavIcon d={Icon.analytics} /> Analytics
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
