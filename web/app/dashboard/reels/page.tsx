import { createClient } from "@/lib/supabase/server";
import { TaskRepository } from "@/lib/vrf";
import { Discover } from "@/components/reels/Discover";

export const dynamic = "force-dynamic";

export default async function DiscoverPage() {
  const supabase = await createClient();
  let latest = null;
  try {
    latest = await new TaskRepository(supabase).latest("discover");
  } catch {
    // The panel still renders and reports its own failures.
  }

  return (
    <>
      <div
        className="page-head"
        data-art="reels"
        style={{ ["--head-art" as string]: "url('/channels/reels.jpg')" }}
      >
        <h1>Discover</h1>
        <p>
          Find short-form video that is performing now, scored on reach against
          audience size. Results are reference material: saving one records what
          was public about it, and grants no rights to the video itself.
        </p>
      </div>
      <Discover
        initial={
          latest
            ? {
                id: latest.id,
                status: latest.status,
                payload: latest.payload,
                result: latest.result as never,
                error: latest.error,
              }
            : null
        }
      />
    </>
  );
}
