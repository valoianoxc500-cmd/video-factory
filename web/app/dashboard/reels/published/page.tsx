import { createClient } from "@/lib/supabase/server";
import { PublishJobRepository } from "@/lib/vrf";
import { JobTable } from "@/components/reels/JobTable";

export const dynamic = "force-dynamic";

export default async function PublishedPage() {
  const supabase = await createClient();
  const jobs = await new PublishJobRepository(supabase).listForUser("published");

  return (
    <>
      <div
        className="page-head"
        data-art="reels"
        style={{ ["--head-art" as string]: "url('/channels/reels.jpg')" }}
      >
        <h1>Published</h1>
        <p>
          Posts the platform actually accepted. A TikTok post from an unaudited
          app lands in your drafts rather than on your profile, and says so here
          instead of claiming it went live.
        </p>
      </div>
      <JobTable
        initial={jobs}
        cancellable={false}
        emptyTitle="Nothing published yet"
        emptyBody="Posts appear here once a platform confirms them."
      />
    </>
  );
}
