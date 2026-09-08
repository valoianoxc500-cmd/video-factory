import { createClient } from "@/lib/supabase/server";
import { AssetRepository, TaskRepository } from "@/lib/vrf";
import { ClipAnalyzer } from "@/components/ClipAnalyzer";
import type { AssetRow, TaskRow } from "@/lib/vrf";

export const dynamic = "force-dynamic";

export default async function AnalyzerPage() {
  const supabase = await createClient();

  let assets: AssetRow[] = [];
  let latest: TaskRow | null = null;
  try {
    assets = await new AssetRepository(supabase).listForUser();
  } catch {
    // An empty list is the correct empty state; the page still renders.
  }
  try {
    latest = await new TaskRepository(supabase).latest("explain");
  } catch {
    latest = null;
  }

  return (
    <div className="surface" data-surface="analyzer">
      <div
        className="page-head engine-head"
        data-engine="analyzer"
        style={{ ["--head-art" as string]: "url('/channels/analytics.jpg')" }}
      >
        <span className="engine-kicker">Study</span>
        <h1>
          Clip <span className="hl">Analyzer</span>
        </h1>
        <p>
          Why a video performed — hook, structure, pacing, visuals, captions and
          audio — and a step-by-step plan to make one like it.
        </p>
      </div>
      <ClipAnalyzer assets={assets} latest={latest} />
    </div>
  );
}
