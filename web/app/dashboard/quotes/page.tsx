import { QuoteStudio } from "@/components/QuoteStudio";

export const dynamic = "force-dynamic";

/**
 * Quote Studio.
 *
 * A standalone product beside the video sections, not a mode of one. It
 * queues no job and reads no channel config: a carousel is finished in
 * seconds and never reaches `factory.py`.
 */
export default function QuoteStudioPage() {
  return (
    <div className="surface" data-surface="quotes">
      <div className="page-head engine-head qs-head" data-engine="quote_studio">
        <span className="engine-kicker">Quote Studio</span>
        <h1>
          One photograph, <span className="hl">a whole carousel</span>
        </h1>
        <p>
          Upload a portrait, name a topic, and get a consistent five-to-eight
          slide quote deck — the same face throughout, typeset properly in
          Arabic or English, and ready to post.
        </p>
      </div>
      <QuoteStudio />
    </div>
  );
}
