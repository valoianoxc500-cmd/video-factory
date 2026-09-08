import { engineBySlug } from "@/lib/engines";

/**
 * The masthead for one engine's screen.
 *
 * Each engine gets its own photograph and its own accent, so the channel you
 * are working in is obvious before you have read a word. The art is real
 * photography (Pexels, credited in public/channels/CREDITS.txt) rather than a
 * gradient, because this is a product about sourcing real footage and the
 * chrome should not be the one place that fakes it.
 */

const ART: Record<
  string,
  { art: string; kicker: string; lead: string; tail: string; blurb: string }
> = {
  football_news: {
    art: "football_news.jpg",
    kicker: "Football",
    lead: "Football",
    tail: "News",
    blurb:
      "Researched against live squad and transfer data, then written, narrated and illustrated.",
  },
  horror_stories: {
    art: "horror_stories.jpg",
    kicker: "Story To Video",
    lead: "Horror",
    tail: "Stories",
    blurb:
      "Paranormal accounts, urban legends and original horror — told in one unmistakable voice.",
  },
  true_stories: {
    // A peak lost in cloud: a real photograph of something genuinely
    // unresolved, which is what this channel is about. Shares the file with
    // the legacy create screen rather than inventing art that does not exist.
    art: "create.jpg",
    kicker: "Story To Video",
    lead: "True",
    tail: "Stories",
    blurb:
      "Real cases. Verified facts stated plainly, claims attributed, and no resolution the case does not have.",
  },
};

export function EngineHead({ engine }: { engine: string }) {
  const meta = ART[engine];
  const info = engineBySlug(engine);
  if (!meta) return null;

  return (
    <div
      className="page-head engine-head"
      data-engine={engine}
      style={{ ["--head-art" as string]: `url('/channels/${meta.art}')` }}
    >
      <span className="engine-kicker">{meta.kicker}</span>
      <h1>
        {meta.lead} <span className="hl">{meta.tail}</span>
      </h1>
      <p>{meta.blurb}</p>
      <span className="sr-only">{info.headline}</span>
    </div>
  );
}
