/**
 * The carousel model: one connected piece, and old projects that still open.
 *
 * The properties worth pinning are the ones that separate a carousel from the
 * quote generator it replaced. Slides carry a role, roles follow position, an
 * edit or a rewrite touches exactly one slide, and a project saved before any
 * of this existed still reads back and still renders.
 *
 * Offline: no network, no model, no paid call.
 *
 * Run with: npm run test  (compiles lib/ first -- see package.json)
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const carousel = require("../.test-build/carousel.js");
const publish = require("../.test-build/carousel-publish.js");

const slide = (text, role, background = "pure_white") => ({
  id: `id-${text}`,
  text,
  role,
  background,
});

const DECK = [
  slide("How to actually become a better person", "hook"),
  slide("Stop trying to impress everyone.", "body"),
  slide("Keep promises to yourself.", "body"),
  slide("Learn to admit when you are wrong.", "body"),
  slide("Become 1% better every day. Save this for later.", "cta"),
];

// ── shape: hook, middle, close ───────────────────────────────────────

test("a generated carousel opens with a hook and closes with a CTA", () => {
  const slides = carousel.normaliseSlides([
    "Why you always feel tired",
    "You are not sleeping badly. You are recovering badly.",
    "Caffeine after 2pm borrows energy you have to repay.",
    "Light in the morning sets the clock for the whole day.",
    "Pick one and start tonight.",
  ]);
  assert.equal(slides.length, 5);
  assert.equal(slides[0].role, "hook");
  assert.equal(slides[slides.length - 1].role, "cta");
  assert.ok(slides.slice(1, -1).every((s) => s.role === "body"));
});

test("the slide count stays within five and eight", () => {
  assert.equal(carousel.MIN_SLIDES, 5);
  assert.equal(carousel.MAX_SLIDES, 8);
  const tooMany = carousel.normaliseSlides(Array.from({ length: 20 }, (_, i) => `s${i}`));
  assert.equal(tooMany.length, 8);
});

test("roleForIndex describes the whole deck", () => {
  assert.equal(carousel.roleForIndex(0, 5), "hook");
  assert.equal(carousel.roleForIndex(2, 5), "body");
  assert.equal(carousel.roleForIndex(4, 5), "cta");
});

test("blank lines from the model are dropped, not rendered empty", () => {
  const slides = carousel.normaliseSlides(["a hook", "", "   ", null, "a close"]);
  assert.equal(slides.length, 2);
  assert.ok(slides.every((s) => s.text.trim()));
});

test("a non-array response yields nothing rather than throwing", () => {
  for (const bad of [null, undefined, "text", 42, {}]) {
    assert.deepEqual(carousel.normaliseSlides(bad), []);
  }
});

// ── reordering re-derives roles ──────────────────────────────────────

test("moving the closing slide to the front makes it the hook", () => {
  const reordered = [DECK[4], ...DECK.slice(0, 4)];
  const fixed = carousel.withRoles(reordered);
  assert.equal(fixed[0].role, "hook");
  assert.equal(fixed[fixed.length - 1].role, "cta");
  // Exactly one of each, or the deck has two endings and no opening.
  assert.equal(fixed.filter((s) => s.role === "hook").length, 1);
  assert.equal(fixed.filter((s) => s.role === "cta").length, 1);
});

test("reordering preserves every slide's text and background", () => {
  const reordered = carousel.withRoles([DECK[2], DECK[0], DECK[1], DECK[3], DECK[4]]);
  assert.deepEqual(
    reordered.map((s) => s.text).sort(),
    DECK.map((s) => s.text).sort(),
  );
});

test("deleting a slide re-derives the roles of the rest", () => {
  const without = carousel.withRoles(DECK.filter((_, i) => i !== 4));
  assert.equal(without.length, 4);
  assert.equal(without[without.length - 1].role, "cta");
});

// ── duplicate ideas ──────────────────────────────────────────────────

test("a repeated idea is detected even when reworded", () => {
  const repeated = [
    slide("Keep your promises", "hook"),
    slide("Keep the promises!", "body"),
    slide("Something else entirely", "cta"),
  ];
  assert.equal(carousel.duplicateIdeas(repeated).length, 1);
});

test("a well-written carousel reports no duplicates", () => {
  assert.deepEqual(carousel.duplicateIdeas(DECK), []);
});

// ── text fits a graphic ──────────────────────────────────────────────

test("a hook is held tighter than a body slide", () => {
  const long = "x".repeat(150);
  assert.equal(carousel.slideFits(long, "hook", "en"), false);
  assert.equal(carousel.slideFits(long, "body", "en"), true);
});

test("Arabic is allowed fewer characters for the same slide", () => {
  const text = "x".repeat(100);
  assert.equal(carousel.slideFits(text, "hook", "en"), true);
  assert.equal(carousel.slideFits(text, "hook", "ar"), false);
});

test("an empty slide never fits", () => {
  assert.equal(carousel.slideFits("", "body", "en"), false);
  assert.equal(carousel.slideFits("   ", "body", "en"), false);
});

// ── backward compatibility ───────────────────────────────────────────

test("an old quote project opens as a carousel", () => {
  // Exactly the shape the old studio stored: no role, no background.
  const oldCards = [
    { id: "a", text: "Motivation fades. Discipline builds.", selected: true, position: 0 },
    { id: "b", text: "Wanting it is easy.", selected: true, position: 1 },
    { id: "c", text: "Do it daily.", selected: true, position: 2 },
  ];
  const slides = carousel.slidesFromCards(oldCards, "soft_cream");

  assert.equal(slides.length, 3);
  assert.equal(slides[0].role, "hook");
  assert.equal(slides[2].role, "cta");
  // The project's background is inherited, which is what those decks rendered
  // with before slides could differ.
  assert.ok(slides.every((s) => s.background === "soft_cream"));
  assert.deepEqual(slides.map((s) => s.text), oldCards.map((c) => c.text));
});

test("stored cards are read back in their saved order", () => {
  const shuffled = [
    { id: "c", text: "third", selected: true, position: 2 },
    { id: "a", text: "first", selected: true, position: 0 },
    { id: "b", text: "second", selected: true, position: 1 },
  ];
  assert.deepEqual(
    carousel.slidesFromCards(shuffled).map((s) => s.text),
    ["first", "second", "third"],
  );
});

test("a round trip through storage preserves the carousel", () => {
  const cards = carousel.cardsFromSlides(DECK);
  const back = carousel.slidesFromCards(cards);
  assert.deepEqual(back.map((s) => s.text), DECK.map((s) => s.text));
  assert.deepEqual(back.map((s) => s.role), DECK.map((s) => s.role));
  assert.deepEqual(back.map((s) => s.background), DECK.map((s) => s.background));
});

test("stored cards keep the fields the old format had", () => {
  // An older reader must still find id, text, selected and position.
  for (const card of carousel.cardsFromSlides(DECK)) {
    assert.ok(typeof card.id === "string" && card.id);
    assert.ok(typeof card.text === "string");
    assert.equal(card.selected, true);
    assert.ok(Number.isInteger(card.position));
  }
});

test("a project saved with an unknown background degrades to white", () => {
  const slides = carousel.slidesFromCards(
    [{ id: "a", text: "x", selected: true, position: 0, background: "retired_marble" }],
  );
  assert.equal(slides[0].background, "pure_white");
});

// ── options ──────────────────────────────────────────────────────────

test("the tones offered are the tones accepted", () => {
  for (const tone of carousel.TONES) {
    assert.ok(carousel.isTone(tone.id), tone.id);
  }
  assert.equal(carousel.isTone("sarcastic"), false);
  assert.equal(carousel.isTone(null), false);
});

test("auto is offered for both language and slide count", () => {
  assert.ok(carousel.CAROUSEL_LANGUAGES.some((l) => l.id === "auto"));
  assert.ok(carousel.SLIDE_COUNTS.some((c) => c.id === "auto"));
});

// ── publishing capability ────────────────────────────────────────────

test("no platform is claimed as implemented", () => {
  // The existing adapters are video-only; claiming otherwise would be the
  // one failure this matrix exists to prevent.
  for (const spec of publish.CAROUSEL_PLATFORMS) {
    assert.equal(spec.implemented, false, `${spec.platform} claims support`);
    assert.ok(spec.missing.trim(), `${spec.platform} must say what is missing`);
  }
  assert.equal(publish.anyPlatformImplemented(), false);
});

test("platform carousel limits reflect the real APIs", () => {
  assert.equal(publish.capability("instagram").maxImages, 10);
  assert.equal(publish.capability("facebook").maxImages, 10);
  assert.equal(publish.capability("threads").maxImages, 20);
  // X has no carousel: four images on one post.
  assert.equal(publish.capability("x").maxImages, 4);
  assert.equal(publish.capability("x").supportsCarousel, false);
});

test("a deck longer than a platform allows is flagged before selection", () => {
  assert.match(publish.coverageNote("x", 7), /4 images/);
  assert.equal(publish.coverageNote("instagram", 7), "");
});

test("only instagram and facebook already have oauth", () => {
  assert.equal(publish.capability("instagram").oauthReady, true);
  assert.equal(publish.capability("facebook").oauthReady, true);
  assert.equal(publish.capability("threads").oauthReady, false);
  assert.equal(publish.capability("x").oauthReady, false);
});

// ── connection state is real ─────────────────────────────────────────

test("connection state comes from the account rows", () => {
  const states = publish.connectionStates([
    { platform: "instagram", account_handle: "@me", revoked_at: null },
  ]);
  const instagram = states.find((s) => s.platform === "instagram");
  assert.equal(instagram.connected, true);
  assert.equal(instagram.handle, "@me");
  assert.equal(states.find((s) => s.platform === "x").connected, false);
});

test("a revoked account is not connected", () => {
  const states = publish.connectionStates([
    { platform: "facebook", account_handle: "@page", revoked_at: "2026-01-01T00:00:00Z" },
  ]);
  assert.equal(states.find((s) => s.platform === "facebook").connected, false);
});

test("no accounts means nothing is connected", () => {
  for (const state of publish.connectionStates([])) {
    assert.equal(state.connected, false);
  }
});

// ── publishing is gated three ways ───────────────────────────────────

test("a disconnected platform cannot publish", () => {
  const [state] = publish.connectionStates([]).filter((s) => s.platform === "instagram");
  const result = publish.canPublish(state, true);
  assert.equal(result.allowed, false);
  assert.match(result.reason, /Connect Instagram/);
});

test("a connected but unimplemented platform still cannot publish", () => {
  const [state] = publish
    .connectionStates([{ platform: "instagram", account_handle: "@me", revoked_at: null }])
    .filter((s) => s.platform === "instagram");
  const result = publish.canPublish(state, true);
  assert.equal(result.allowed, false);
  assert.match(result.reason, /not available yet/);
});

test("publishing requires explicit confirmation", () => {
  const [state] = publish
    .connectionStates([{ platform: "instagram", account_handle: "@me", revoked_at: null }])
    .filter((s) => s.platform === "instagram");
  // Even with everything else satisfied, confirmed=false is refused.
  assert.equal(publish.canPublish(state, false).allowed, false);
});

// ── captions ─────────────────────────────────────────────────────────

test("a caption is trimmed to each platform's limit", () => {
  const long = "This is a sentence. ".repeat(60);
  for (const [platform, limit] of Object.entries(carousel.CAPTION_LIMITS)) {
    const shaped = publish.adaptCaption(long, platform, limit);
    assert.ok(shaped.length <= limit, `${platform}: ${shaped.length} > ${limit}`);
  }
});

test("a short caption is left exactly as written", () => {
  const caption = "Save this for later.";
  assert.equal(publish.adaptCaption(caption, "x", 280), caption);
});

test("a trimmed caption ends on a finished thought where it can", () => {
  const text = "First sentence here. Second sentence here. Third one runs past the limit.";
  const shaped = publish.adaptCaption(text, "x", 45);
  assert.ok(shaped.length <= 45);
  assert.ok(!shaped.endsWith(" "));
});

test("an empty caption stays empty", () => {
  assert.equal(publish.adaptCaption("", "x", 280), "");
  assert.equal(publish.adaptCaption(null, "x", 280), "");
});
