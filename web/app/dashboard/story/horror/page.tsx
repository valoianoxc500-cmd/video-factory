import { createClient } from "@/lib/supabase/server";
import { JobRepository } from "@/lib/repositories";
import { CreateVideo } from "@/components/CreateVideo";
import { EngineHead } from "@/components/EngineHead";

export const dynamic = "force-dynamic";

export default async function HorrorStoriesPage() {
  const supabase = await createClient();
  const active = await new JobRepository(supabase).activeForUser();

  return (
    <div className="surface" data-surface="horror">
      <EngineHead engine="horror_stories" />
      <CreateVideo activeJob={active} engine="horror_stories" />
    </div>
  );
}
