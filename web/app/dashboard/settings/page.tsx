import Link from "next/link";
import { getUser, createClient } from "@/lib/supabase/server";
import { branding } from "@/lib/branding";
import { VideoRepository, JobRepository } from "@/lib/repositories";

export const dynamic = "force-dynamic";

export default async function SettingsPage() {
  const user = await getUser();
  const supabase = await createClient();
  const [videos, jobs] = await Promise.all([
    new VideoRepository(supabase).listForUser(),
    new JobRepository(supabase).listForUser(200),
  ]);

  // Read directly rather than through a repository: this is one count for a
  // link label, and a failure here must not take the settings page down.
  //
  // A live connection is a row with no revoked_at. There is no `status`
  // column -- selecting one makes PostgREST reject the whole query, which the
  // catch below then swallowed, so this count read 0 however many accounts
  // were actually connected. The same mistake was already fixed once in
  // /api/reels/assets.
  let connected = 0;
  try {
    const { data, error } = await supabase
      .from("vrf_accounts")
      .select("platform, revoked_at")
      .is("revoked_at", null);
    if (error) throw new Error(error.message);
    connected = (data ?? []).length;
  } catch {
    /* the link still works without the count */
  }

  return (
    <>
      <div
        className="page-head"
        data-art="create"
        style={{ ["--head-art" as string]: "url('/channels/create.jpg')" }}
      >
        <h1>Account</h1>
        <p>Your workspace and usage.</p>
      </div>

      <div className="stat-grid">
        <div className="stat"><div className="k">Email</div><div className="v" style={{ fontSize: 15, wordBreak: "break-all" }}>{user?.email}</div></div>
        <div className="stat"><div className="k">Videos generated</div><div className="v">{videos.length}</div></div>
        <div className="stat"><div className="k">Generations run</div><div className="v">{jobs.length}</div></div>
      </div>

      {/* Connecting a social account is an integration, so it belongs with
          the account rather than in the main nav. */}
      <div className="sec-head" style={{ marginTop: 30 }}>
        <h2>Integrations</h2>
      </div>

      <div className="tile-grid">
        <Link href="/dashboard/reels/accounts" className="tile">
          <span className="tile-icon" aria-hidden>
            <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
              <path
                d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Zm13 10v-2a4 4 0 0 0-3-3.9"
                stroke="currentColor"
                strokeWidth="1.8"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <span className="tile-body">
            <strong>Connected accounts</strong>
            <small>
              {connected > 0
                ? `${connected} connected`
                : "Connect TikTok, Instagram or Facebook to publish"}
            </small>
          </span>
        </Link>
      </div>

      <div className="sec-head" style={{ marginTop: 30 }}>
        <h2>Plan</h2>
        <Link className="sec-link" href="/dashboard/plans">
          See credit plans
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden>
            <path
              d="M5 12h14m-6-7 7 7-7 7"
              stroke="currentColor"
              strokeWidth="1.9"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </Link>
      </div>

      <div className="stat" style={{ marginTop: 6 }}>
        <div className="k">Current plan</div>
        <div className="v" style={{ fontSize: 17 }}>Unlimited (preview)</div>
        <p style={{ color: "var(--sa-dim)", fontSize: 13.5, marginTop: 8, lineHeight: 1.6 }}>
          Subscriptions, credits and usage limits are not enabled yet. Usage is
          being recorded so plans can be introduced without losing history.
        </p>
      </div>

      <p style={{ color: "#5b6076", fontSize: 12.5, marginTop: 26 }}>
        {branding.appName} · {branding.tagline}
      </p>
    </>
  );
}
