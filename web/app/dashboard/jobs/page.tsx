import { createClient } from "@/lib/supabase/server";
import { JobRepository } from "@/lib/repositories";
import { JobList } from "@/components/JobList";

export const dynamic = "force-dynamic";

export default async function JobsPage() {
  const supabase = await createClient();
  const jobs = await new JobRepository(supabase).listForUser();

  return (
    <>
      <div
        className="page-head"
        data-art="analytics"
        style={{ ["--head-art" as string]: "url('/channels/analytics.jpg')" }}
      >
        <h1>Jobs &amp; Activity</h1>
        <p>Live progress for every generation you have started.</p>
      </div>
      <JobList initial={jobs} />
    </>
  );
}
