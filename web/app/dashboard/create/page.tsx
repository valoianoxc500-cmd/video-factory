import { createClient } from "@/lib/supabase/server";
import { JobRepository } from "@/lib/repositories";
import { CreateVideo } from "@/components/CreateVideo";

export const dynamic = "force-dynamic";

export default async function CreatePage() {
  const supabase = await createClient();
  const active = await new JobRepository(supabase).activeForUser();

  return (
    <>
      <div
        className="page-head create-head"
        data-art="create"
        style={{ ["--head-art" as string]: "url('/channels/create.jpg')" }}
      >
        <h1>
          Create <span className="hl">Video</span>
        </h1>
        <p>Turn any topic into a finished video with AI.</p>

        {/*
          The one handwritten mark in the product. Everything else here is
          grid and rectangles; this is the note a designer scribbles in the
          margin of a comp, and it says what the tool is for in three words.
          It appears on this screen only.
        */}
        <div className="scribble" aria-hidden>
          <span>Ideas</span>
          <span>Become</span>
          <span>Videos</span>
          <svg width="118" height="12" viewBox="0 0 118 12" fill="none">
            <path
              d="M2 8.5C28 3.5 78 1.5 116 6"
              stroke="currentColor"
              strokeWidth="2.4"
              strokeLinecap="round"
            />
          </svg>
        </div>
      </div>
      <CreateVideo activeJob={active} />
    </>
  );
}
