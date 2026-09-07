/**
 * Generation engines the UI offers and the API accepts.
 *
 * An engine is a channel config in `config/channels/<slug>.json` plus whatever
 * extra pipeline behaviour that channel turns on. Adding one means adding a
 * channel JSON on the worker and one entry here; no API, worker or UI code
 * changes.
 *
 * `slug` MUST match the channel config filename, because the worker passes it
 * straight to `factory.py --channel`.
 */

export type StoryStyle = {
  slug: string;
  label: string;
  hint: string;
};

export type ScriptLanguage = {
  code: string;
  label: string;
  /** Written in the language itself, so the choice is self-evident. */
  native: string;
};

export type Engine = {
  slug: string;
  label: string;
  /** Page headline while this engine is selected. */
  headline: string;
  /** Sentence under the headline, completed by the shared render blurb. */
  sub: string;
  /** Short line under the mode switch. */
  blurb: string;
  inputLabel: string;
  placeholder: string;
  submitLabel: string;
  examples: string[];
  /** Optional sub-mode. Sent as `style` and reaches the pipeline as plan.story_type. */
  styles?: StoryStyle[];
  /**
   * Script languages this engine offers. Controls narration, captions and the
   * finished video; visual search always runs in both scripts regardless.
   */
  languages?: ScriptLanguage[];
  /** Applies the cinematic horror theme to the page. */
  theme?: "horror";
};

export const ENGINES: Engine[] = [
  {
    slug: "football_news",
    label: "Football News",
    headline: "Arabic football news, generated end to end",
    sub:
      "Enter a topic and the pipeline researches it, writes an Arabic script, " +
      "and finds real web photographs of the actual people and events.",
    blurb:
      "Researches a football story, writes an Arabic script, and illustrates it with real photographs.",
    inputLabel: "Football topic or news",
    placeholder: "e.g. Mbappé's move to Real Madrid",
    submitLabel: "Generate",
    examples: [
      "صفقات الانتقالات الصيفية الكبرى",
      "أعظم لحظات دوري أبطال أوروبا",
      "لماذا يتألق حارس مرمى مانشستر سيتي؟",
    ],
  },
  {
    slug: "horror_stories",
    label: "Horror Stories",
    headline: "Arabic horror, told end to end",
    sub:
      "Name a story and the pipeline researches it, separates what is verified " +
      "from what is merely claimed, writes a complete Arabic arc, and finds real " +
      "photographs of the actual places.",
    blurb:
      "Researches the case, marks fact apart from rumour, and builds a complete cinematic Arabic story.",
    inputLabel: "Story or case",
    placeholder: "e.g. اختفاء عائلة سوديف",
    submitLabel: "Generate Story",
    theme: "horror",
    languages: [
      { code: "ar", label: "Arabic", native: "العربية" },
      { code: "en", label: "English", native: "English" },
    ],
    styles: [
      {
        slug: "true_story",
        label: "True Story",
        hint: "A real case. Researched first; verified facts stated plainly, claims attributed, nothing invented.",
      },
      {
        slug: "paranormal",
        label: "Paranormal",
        hint: "Accounts of the unexplained, narrated as what witnesses claim rather than as established fact.",
      },
      {
        slug: "urban_legend",
        label: "Urban Legend",
        hint: "Folklore and rumour, told as the story people pass on, with its origins made clear.",
      },
      {
        slug: "custom_horror",
        label: "Custom Horror",
        hint: "Original fiction. Never dressed as a real case, place or crime.",
      },
    ],
    examples: [
      "اختفاء عائلة سوديف",
      "أسطورة بئر برهوت",
      "قصة القصر المهجور في الصحراء",
    ],
  },
];

export const DEFAULT_ENGINE = ENGINES[0].slug;

export function isKnownEngine(slug: string): boolean {
  return ENGINES.some((e) => e.slug === slug);
}

export function engineBySlug(slug: string): Engine {
  return ENGINES.find((e) => e.slug === slug) ?? ENGINES[0];
}

/** Whether `style` is a valid sub-mode of `engine`. Empty style is allowed. */
export function isKnownStyle(engineSlug: string, style: string): boolean {
  if (!style) return true;
  const engine = ENGINES.find((e) => e.slug === engineSlug);
  return (engine?.styles ?? []).some((s) => s.slug === style);
}

export function defaultStyle(engineSlug: string): string {
  const engine = ENGINES.find((e) => e.slug === engineSlug);
  return engine?.styles?.[0]?.slug ?? "";
}

/** Whether `code` is a script language this engine offers. Empty is allowed. */
export function isKnownLanguage(engineSlug: string, code: string): boolean {
  if (!code) return true;
  const engine = ENGINES.find((e) => e.slug === engineSlug);
  return (engine?.languages ?? []).some((l) => l.code === code);
}

export function defaultLanguage(engineSlug: string): string {
  const engine = ENGINES.find((e) => e.slug === engineSlug);
  return engine?.languages?.[0]?.code ?? "";
}
