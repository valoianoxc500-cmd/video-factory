import { createClient } from "@/lib/supabase/server";
import { QuoteStudioRepository } from "@/lib/repositories";
import { QuoteStudio } from "@/components/QuoteStudio";
import type { QuoteProject } from "@/lib/quotes";

export const dynamic = "force-dynamic";

/**
 * Quote Studio.
 *
 * A standalone product beside the video sections, not a mode of one. It
 * queues no job and reads no channel config: a deck is rendered locally and
 * never reaches `factory.py`.
 *
 * The most recent project is loaded here so the studio opens on the surface,
 * typeface and quotes it was last saved with. A workspace with no projects
 * yet simply opens empty -- and a storage error opens empty too, because a
 * studio that cannot list old work is still a studio that can make new work.
 */
export default async function QuoteStudioPage() {
  let latest: QuoteProject | null = null;
  try {
    const supabase = await createClient();
    const { data } = await supabase.auth.getUser();
    if (data.user) {
      const projects = await new QuoteStudioRepository(
        supabase,
        data.user.id,
      ).listProjects(1);
      latest = projects[0] ?? null;
    }
  } catch {
    latest = null;
  }

  return (
    <div className="surface" data-surface="quotes">
      <div className="page-head engine-head qs-head" data-engine="quote_studio">
        <span className="engine-kicker">Quote Studio</span>
        <h1>
          One idea, <span className="hl">a whole carousel</span>
        </h1>
        <p>
          Write the quotes, choose a surface and a typeface, and export a
          five-to-eight slide deck — typeset properly in Arabic or English,
          rendered locally, and ready to post.
        </p>
      </div>
      <QuoteStudio initialProject={latest} />
    </div>
  );
}
