import { createClient } from "@/lib/supabase/server";
import { JobRepository } from "@/lib/repositories";
import { CreateVideo } from "@/components/CreateVideo";
import { EngineHead } from "@/components/EngineHead";

export const dynamic = "force-dynamic";

export default async function FootballPage() {
  const supabase = await createClient();
  const active = await new JobRepository(supabase).activeForUser();

  return (
    // The accent is set here rather than on the masthead, so the controls and
    // the generate button below carry the channel's colour too.
    <div className="surface" data-surface="football">
      <EngineHead engine="football_news" />
      <CreateVideo activeJob={active} engine="football_news" />
    </div>
  );
}
