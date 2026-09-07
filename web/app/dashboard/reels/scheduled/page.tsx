import { createClient } from "@/lib/supabase/server";
import { PublishJobRepository } from "@/lib/vrf";
import { JobTable } from "@/components/reels/JobTable";

export const dynamic = "force-dynamic";

export default async function ScheduledPage() {
  const supabase = await createClient();
  const jobs = await new PublishJobRepository(supabase).scheduled();

  return (
    <>
      <div
        className="page-head"
        data-art="reels"
        style={{ ["--head-art" as string]: "url('/channels/reels.jpg')" }}
      >
        <h1>Scheduled</h1>
        <p>
          Posts with a time on them. YouTube and Facebook are handed the time at
          upload and publish it themselves; Instagram and TikTok have no native
          scheduling, so those are held here and posted when they are due.
        </p>
      </div>
      <JobTable
        initial={jobs}
        emptyTitle="Nothing scheduled"
        emptyBody="Set a time when you publish a video and it will wait here."
      />
    </>
  );
}
