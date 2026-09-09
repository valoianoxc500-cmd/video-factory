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

/**
 * The nav groups an engine can belong to.
 *
 * Football stands alone; the two story engines share "Story To Video" because
 * they are the same act -- a researched story turned into a narrated video --
 * differing only in what they promise about the truth of it.
 */
export type EngineSection = "football" | "story" | "animated";

export const SECTIONS: { id: EngineSection; label: string }[] = [
  { id: "football", label: "Football" },
  { id: "story", label: "Story To Video" },
  // A third standalone section, not a mode of the other two: an animated
  // story is drawn and animated rather than sourced and narrated over
  // photographs, and it runs its own generation path.
  { id: "animated", label: "Animated Stories" },
];

export type Engine = {
  slug: string;
  label: string;
  section: EngineSection;
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
   * Languages this engine can *narrate* in. Selecting one swaps the channel's
   * language variant, which moves the narration language, the voice and the
   * script instructions together -- they cannot move independently.
   */
  voiceLanguages?: ScriptLanguage[];
  /**
   * Languages the caption track can be written in. Where this differs from the
   * chosen voice language the captions are translated and aligned to the
   * narration's measured sentence timings, so a line still appears and leaves
   * exactly when its sentence is spoken.
   *
   * Every engine offers both, including ones that narrate in only one
   * language: reading English under Arabic narration is the point.
   */
  captionLanguages?: ScriptLanguage[];
  /** Applies the cinematic horror theme to the page. */
  theme?: "horror" | "true" | "football" | "animated";
  /** Scene lengths this engine offers, in seconds. Animated engines only. */
  sceneDurations?: number[];
  /** Whether the page offers character-style controls. */
  hasCharacterStyle?: boolean;
  /** Which narrator this engine speaks with, stated plainly in the UI. */
  voiceNote?: string;
};

const AR: ScriptLanguage = { code: "ar", label: "Arabic", native: "العربية" };
const EN: ScriptLanguage = { code: "en", label: "English", native: "English" };

/** Both engines caption in either script; kept in one place so they cannot drift. */
const CAPTION_LANGUAGES: ScriptLanguage[] = [AR, EN];

export const ENGINES: Engine[] = [
  {
    slug: "football_news",
    label: "Football News",
    section: "football",
    theme: "football",
    headline: "Arabic football news, generated end to end",
    sub:
      "Enter a topic and the pipeline researches it, writes an Arabic script, " +
      "and finds real web photographs of the actual people and events.",
    blurb:
      "Researches a football story, writes an Arabic script, and illustrates it with real photographs.",
    inputLabel: "Football topic or news",
    placeholder: "e.g. Mbappé's move to Real Madrid",
    submitLabel: "Generate",
    // The channel declares only an Arabic variant, and asking it for English
    // narration fails in the worker rather than here. Captions can still be
    // English, which is what the caption control is for.
    voiceLanguages: [AR],
    captionLanguages: CAPTION_LANGUAGES,
    examples: [
      "صفقات الانتقالات الصيفية الكبرى",
      "أعظم لحظات دوري أبطال أوروبا",
      "لماذا يتألق حارس مرمى مانشستر سيتي؟",
    ],
  },
  {
    slug: "horror_stories",
    label: "Horror Stories",
    section: "story",
    theme: "horror",
    headline: "Horror, told end to end",
    sub:
      "Name a story and the pipeline researches it, separates what is verified " +
      "from what is merely claimed, writes a complete arc, and finds real " +
      "photographs of the actual places.",
    blurb:
      "Paranormal accounts, urban legends and original horror, narrated in one unmistakable voice.",
    inputLabel: "Story or case",
    placeholder: "e.g. أسطورة بئر برهوت",
    submitLabel: "Generate Story",
    voiceNote: "Narrated by Rudra — Intense Documentary Narrator (ElevenLabs).",
    voiceLanguages: [AR, EN],
    captionLanguages: CAPTION_LANGUAGES,
    styles: [
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
      "أسطورة بئر برهوت",
      "قصة القصر المهجور في الصحراء",
      "حكايات الطريق الصحراوي القديم",
    ],
  },
  {
    slug: "true_stories",
    label: "True Stories",
    section: "story",
    theme: "true",
    headline: "Real cases, told exactly as they are known",
    sub:
      "Name a real case and the pipeline researches it first, states verified " +
      "facts plainly, attributes what was merely reported, and never " +
      "manufactures a resolution the case does not have.",
    blurb:
      "A real case, researched first: verified facts stated plainly, claims attributed, nothing invented.",
    inputLabel: "Case or event",
    placeholder: "e.g. اختفاء عائلة سوديف",
    submitLabel: "Generate Story",
    voiceNote: "Narrated by the documentary voice this channel has always used.",
    voiceLanguages: [AR, EN],
    captionLanguages: CAPTION_LANGUAGES,
    // One story type, and it is the whole promise of the engine. Sent
    // explicitly so the pipeline applies the true_story truth rules rather
    // than inferring them from the channel name.
    styles: [
      {
        slug: "true_story",
        label: "True Story",
        hint: "A real case. Researched first; verified facts stated plainly, claims attributed, nothing invented.",
      },
    ],
    examples: [
      "اختفاء عائلة سوديف",
      "قضية دي بي كوبر",
      "لغز سفينة ماري سيليست",
    ],
  },
  {
    slug: "animated_stories",
    label: "Animated Stories",
    section: "animated",
    theme: "animated",
    headline: "Your story, drawn and animated",
    sub:
      "Enter a story and the pipeline writes it, designs one character, draws " +
      "every scene in that character's style, and animates the beats that " +
      "carry motion.",
    blurb:
      "Premium stick-figure animation: one consistent character, cinematic backgrounds, real movement.",
    inputLabel: "Story or idea",
    placeholder: "e.g. The night shift nobody else applied for",
    submitLabel: "Create Animated Story",
    voiceNote:
      "Scenes are drawn with FLUX Schnell and animated with a fal.ai image-to-video model.",
    voiceLanguages: [AR, EN],
    captionLanguages: CAPTION_LANGUAGES,
    sceneDurations: [3, 4, 5, 6],
    hasCharacterStyle: true,
    styles: [
      {
        slug: "stick_figure",
        label: "Stick Figure",
        hint: "Large round white heads, expressive faces, thin bodies, colourful clothing, cinematic backgrounds.",
      },
    ],
    examples: [
      "The night shift nobody else applied for",
      "The boy who found a door in the school basement",
      "The delivery driver who kept getting the same address",
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

export function enginesInSection(section: EngineSection): Engine[] {
  return ENGINES.filter((e) => e.section === section);
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

/** Whether `code` is a voice language this engine offers. Empty is allowed. */
export function isKnownLanguage(engineSlug: string, code: string): boolean {
  if (!code) return true;
  const engine = ENGINES.find((e) => e.slug === engineSlug);
  return (engine?.voiceLanguages ?? []).some((l) => l.code === code);
}

export function defaultLanguage(engineSlug: string): string {
  const engine = ENGINES.find((e) => e.slug === engineSlug);
  return engine?.voiceLanguages?.[0]?.code ?? "";
}

/** Whether `code` is a caption language this engine offers. Empty is allowed. */
export function isKnownCaptionLanguage(engineSlug: string, code: string): boolean {
  if (!code) return true;
  const engine = ENGINES.find((e) => e.slug === engineSlug);
  return (engine?.captionLanguages ?? []).some((l) => l.code === code);
}

/**
 * Captions default to the spoken language.
 *
 * That is the only setting where every word timing is measured from the audio
 * rather than interpolated across a translated sentence, so it stays the
 * default and differing from it is a deliberate choice.
 */
export function defaultCaptionLanguage(engineSlug: string): string {
  return defaultLanguage(engineSlug);
}
