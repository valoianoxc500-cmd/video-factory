import { createClient, requireUser } from "@/lib/supabase/server";
import { toHttpError } from "@/lib/repositories";
import { MetricsRepository, PublishJobRepository } from "@/lib/vrf";

/**
 * Performance of the caller's published posts, and how the predictions did.
 *
 * The latest reading per post is what the dashboard shows; older readings are
 * kept so growth over time can be drawn. Only this user's rows are visible.
 */

export const dynamic = "force-dynamic";

export async function GET() {
  try {
    await requireUser();
    const supabase = await createClient();

    const [metrics, published] = await Promise.all([
      new MetricsRepository(supabase).listForUser(),
      new PublishJobRepository(supabase).listForUser("published"),
    ]);

    // Rows arrive newest first, so the first sighting of a job is its latest.
    const latest = new Map<string, (typeof metrics)[number]>();
    for (const row of metrics) {
      if (!latest.has(row.publish_job_id)) latest.set(row.publish_job_id, row);
    }

    const posts = published.map((job) => ({
      jobId: job.id,
      platform: job.platform,
      postUrl: job.post_url,
      publishedAt: job.updated_at,
      metrics: latest.get(job.id) ?? null,
    }));

    const measured = posts.filter((p) => p.metrics);
    const totals = measured.reduce(
      (acc, post) => ({
        views: acc.views + (post.metrics?.views ?? 0),
        likes: acc.likes + (post.metrics?.likes ?? 0),
        comments: acc.comments + (post.metrics?.comments ?? 0),
      }),
      { views: 0, likes: 0, comments: 0 },
    );

    return Response.json({
      posts,
      totals,
      history: metrics,
      // Said plainly rather than implied by an empty chart.
      note:
        measured.length === 0
          ? "No metrics collected yet. Figures appear once a post has been " +
            "live long enough for the platform to report on it."
          : "",
    });
  } catch (err) {
    const { status, message } = toHttpError(err);
    return Response.json({ error: message, posts: [] }, { status });
  }
}
