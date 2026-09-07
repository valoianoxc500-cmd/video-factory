import { createClient } from "@/lib/supabase/server";
import { PublishJobRepository } from "@/lib/vrf";
import { JobTable } from "@/components/reels/JobTable";

export const dynamic = "force-dynamic";

export default async function QueuePage() {
  const supabase = await createClient();
  const jobs = await new PublishJobRepository(supabase).listForUser([
    "queued",
    "processing",
    "failed",
    "unsupported",
  ]);

  return (
    <>
      <div
        className="page-head"
        data-art="reels"
        style={{ ["--head-art" as string]: "url('/channels/reels.jpg')" }}
      >
        <h1>Queue</h1>
        <p>
          Everything waiting to post, retrying, or stopped. A failure that cannot
          succeed on a retry is not retried — repeating a rejected request only
          spends your rate limit.
        </p>
      </div>
      <JobTable
        initial={jobs}
        emptyTitle="Nothing queued"
        emptyBody="Publish a video from My Videos and it will appear here."
      />
    </>
  );
}
