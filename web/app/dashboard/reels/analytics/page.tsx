import { createClient } from "@/lib/supabase/server";
import { MetricsRepository, PLATFORMS, PublishJobRepository } from "@/lib/vrf";

export const dynamic = "force-dynamic";

/**
 * How published posts actually did, and how the predictions compared.
 *
 * Where a platform reports nothing, the cell says so rather than showing a
 * zero — a fabricated zero would read as a real measurement and would also
 * poison the prediction calibration.
 */
export default async function AnalyticsPage() {
  const supabase = await createClient();
  const [metrics, published] = await Promise.all([
    new MetricsRepository(supabase).listForUser(),
    new PublishJobRepository(supabase).listForUser("published"),
  ]);

  const latest = new Map<string, (typeof metrics)[number]>();
  for (const row of metrics) {
    if (!latest.has(row.publish_job_id)) latest.set(row.publish_job_id, row);
  }

  const rows = published.map((job) => ({ job, metric: latest.get(job.id) ?? null }));
  const measured = rows.filter((r) => r.metric);
  const totals = measured.reduce(
    (acc, row) => ({
      views: acc.views + (row.metric?.views ?? 0),
      likes: acc.likes + (row.metric?.likes ?? 0),
      comments: acc.comments + (row.metric?.comments ?? 0),
    }),
    { views: 0, likes: 0, comments: 0 },
  );

  return (
    <>
      <div
        className="page-head"
        data-art="analytics"
        style={{ ["--head-art" as string]: "url('/channels/analytics.jpg')" }}
      >
        <h1>Analytics</h1>
        <p>
          Real numbers from your connected accounts, next to what was predicted
          before you posted.
        </p>
      </div>

      <div className="stat-grid">
        <div className="stat">
          <span className="k">Posts published</span>
          <span className="v">{published.length}</span>
        </div>
        <div className="stat">
          <span className="k">Views</span>
          <span className="v">{compact(totals.views)}</span>
        </div>
        <div className="stat">
          <span className="k">Likes</span>
          <span className="v">{compact(totals.likes)}</span>
        </div>
        <div className="stat">
          <span className="k">Comments</span>
          <span className="v">{compact(totals.comments)}</span>
        </div>
      </div>

      {rows.length === 0 ? (
        <div className="empty">
          <h3>Nothing measured yet</h3>
          <p>
            Figures appear once a post has been live long enough for the platform
            to report on it.
          </p>
        </div>
      ) : (
        <div className="reels-table-wrap">
          <table className="reels-table">
            <thead>
              <tr>
                <th>Platform</th>
                <th>Published</th>
                <th>Views</th>
                <th>Likes</th>
                <th>Comments</th>
                <th>Shares</th>
                <th>Predicted</th>
              </tr>
            </thead>
            <tbody>
              {rows.map(({ job, metric }) => (
                <tr key={job.id}>
                  <td>{label(job.platform)}</td>
                  <td>{new Date(job.updated_at).toLocaleDateString()}</td>
                  <td>{cell(metric?.views)}</td>
                  <td>{cell(metric?.likes)}</td>
                  <td>{cell(metric?.comments)}</td>
                  <td>{cell(metric?.shares)}</td>
                  <td>
                    {metric?.predicted_probability != null
                      ? `${metric.predicted_probability}%`
                      : "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="note">
        A dash means the platform does not report that figure — YouTube, for
        instance, exposes no share count. It is left blank rather than shown as
        zero.
      </p>
    </>
  );
}

function cell(value: number | null | undefined): string {
  return value === null || value === undefined ? "—" : compact(value);
}

function compact(value: number): string {
  return Intl.NumberFormat("en", { notation: "compact" }).format(value);
}

function label(platform: string): string {
  return PLATFORMS.find((p) => p.platform === platform)?.label ?? platform;
}
