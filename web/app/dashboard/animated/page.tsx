import { createClient } from "@/lib/supabase/server";
import { JobRepository } from "@/lib/repositories";
import { AnimatedStudio } from "@/components/AnimatedStudio";

export const dynamic = "force-dynamic";

/**
 * Animated Stories.
 *
 * A third standalone section beside Football and Story To Video, on its own
 * generation path. Nothing here reads or changes the other channels.
 */
export default async function AnimatedStoriesPage() {
  const supabase = await createClient();
  const active = await new JobRepository(supabase).activeForUser();

  return (
    <div className="surface" data-surface="animated">
      <div
        className="page-head engine-head anim-head"
        data-engine="animated_stories"
        style={{ ["--head-art" as string]: "url('/channels/create.jpg')" }}
      >
        <span className="engine-kicker">Animated Stories</span>
        <h1>
          Your story, <span className="hl">drawn and animated</span>
        </h1>
        <p>
          One consistent character, cinematic backgrounds, and real movement on
          the beats that carry it — narrated, captioned and cut to the story.
        </p>
      </div>
      <AnimatedStudio activeJob={active} />
    </div>
  );
}
