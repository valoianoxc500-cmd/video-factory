import { createClient } from "@/lib/supabase/server";
import { SourceRepository } from "@/lib/vrf";
import { SavedList } from "@/components/reels/SavedList";

export const dynamic = "force-dynamic";

export default async function SavedPage() {
  const supabase = await createClient();
  const sources = await new SourceRepository(supabase).listForUser();

  return (
    <>
      <div
        className="page-head"
        data-art="reels"
        style={{ ["--head-art" as string]: "url('/channels/reels.jpg')" }}
      >
        <h1>Viral Videos</h1>
        <p>
          Videos you saved from Discover, with their scores and any analysis.
          These belong to their creators — they are here to learn from, not to
          republish.
        </p>
      </div>
      <SavedList initial={sources} />
    </>
  );
}
