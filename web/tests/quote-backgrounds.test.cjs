/**
 * Quote Studio backgrounds: readability and persistence.
 *
 * Two properties carry the feature. Every background must produce readable
 * ink without anyone choosing that ink by hand -- otherwise adding a swatch
 * later ships an unreadable card. And a stored value must always resolve to
 * something, because a project saved before backgrounds existed has no id and
 * must still open on the surface it was designed against.
 *
 * Run with: npm run test  (compiles lib/ first -- see package.json)
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const bg = require("../.test-build/quote-backgrounds.js");

// ── the library ──────────────────────────────────────────────────────

test("the library holds exactly the offered surfaces", () => {
  assert.deepEqual(
    bg.BACKGROUNDS.map((b) => b.id),
    [
      "pure_white",
      "soft_cream",
      "light_gray",
      "black",
      "editorial_paper",
      "subtle_gradient",
      "minimal_texture",
    ],
  );
});

test("white is the default and comes first", () => {
  assert.equal(bg.DEFAULT_BACKGROUND, "pure_white");
  assert.equal(bg.BACKGROUNDS[0].id, "pure_white");
});

test("every background carries a label, a hint and CSS", () => {
  for (const spec of bg.BACKGROUNDS) {
    assert.ok(spec.label.trim(), `${spec.id} needs a label`);
    assert.ok(spec.hint.trim(), `${spec.id} needs a hint`);
    assert.ok(spec.css.trim(), `${spec.id} needs CSS`);
    assert.equal(spec.base.length, 3, `${spec.id} needs an rgb base`);
  }
});

// ── contrast is derived, not typed in ────────────────────────────────

test("every background produces readable ink", () => {
  for (const spec of bg.BACKGROUNDS) {
    assert.ok(
      bg.meetsContrast(spec),
      `${spec.id} does not reach 3:1 against its own ink`,
    );
  }
});

test("light surfaces get dark ink and dark surfaces get light ink", () => {
  assert.equal(bg.inkFor(bg.backgroundById("pure_white")).isDark, false);
  assert.equal(bg.inkFor(bg.backgroundById("soft_cream")).isDark, false);
  assert.equal(bg.inkFor(bg.backgroundById("black")).isDark, true);
});

test("black and white are near-opposite luminance", () => {
  const white = bg.luminance(bg.backgroundById("pure_white").base);
  const black = bg.luminance(bg.backgroundById("black").base);
  assert.ok(white > 0.9, "white should be near 1");
  assert.ok(black < 0.05, "black should be near 0");
});

test("contrast ratio is symmetric and bounded", () => {
  const a = [255, 255, 255];
  const b = [0, 0, 0];
  assert.equal(
    bg.contrastRatio(a, b).toFixed(4),
    bg.contrastRatio(b, a).toFixed(4),
  );
  assert.ok(bg.contrastRatio(a, b) > 20);
  assert.equal(bg.contrastRatio(a, a).toFixed(2), "1.00");
});

test("the ink palette is complete for every background", () => {
  for (const spec of bg.BACKGROUNDS) {
    const ink = bg.inkFor(spec);
    for (const key of ["ink", "secondary", "rule", "accent", "photoRing"]) {
      assert.ok(String(ink[key] ?? "").trim(), `${spec.id} missing ${key}`);
    }
  }
});

test("the photo ring is never fully transparent", () => {
  // A light photo on a light surface dissolves without it.
  for (const spec of bg.BACKGROUNDS) {
    const ring = bg.inkFor(spec).photoRing;
    assert.ok(/rgba\(/.test(ring));
    assert.ok(!/,\s*0\)$/.test(ring), `${spec.id} ring is invisible`);
  }
});

// ── stored values always resolve ─────────────────────────────────────

test("a saved background is returned unchanged", () => {
  for (const spec of bg.BACKGROUNDS) {
    assert.equal(bg.normaliseBackground(spec.id), spec.id);
  }
});

test("a project saved before backgrounds existed opens on white", () => {
  for (const missing of [null, undefined, ""]) {
    assert.equal(bg.normaliseBackground(missing), "pure_white");
  }
});

test("a background removed from the library degrades to white", () => {
  assert.equal(bg.normaliseBackground("retired_marble"), "pure_white");
});

test("hostile stored values do not throw", () => {
  for (const value of [42, {}, [], true, "__proto__", "constructor"]) {
    assert.equal(bg.normaliseBackground(value), "pure_white");
  }
});

test("backgroundById never returns undefined", () => {
  assert.equal(bg.backgroundById("nope").id, "pure_white");
  assert.equal(bg.backgroundById("black").id, "black");
});

test("isBackgroundId accepts only real ids", () => {
  assert.equal(bg.isBackgroundId("soft_cream"), true);
  assert.equal(bg.isBackgroundId("soft cream"), false);
  assert.equal(bg.isBackgroundId(null), false);
});

// ── export painting ──────────────────────────────────────────────────

test("painting is deterministic for the same surface", () => {
  // Two identical fake contexts must receive identical call sequences, or an
  // exported PNG would differ from the one the user previewed.
  const record = () => {
    const calls = [];
    return {
      calls,
      canvas: {},
      save() { calls.push(["save"]); },
      restore() { calls.push(["restore"]); },
      beginPath() { calls.push(["beginPath"]); },
      moveTo(...a) { calls.push(["moveTo", ...a]); },
      lineTo(...a) { calls.push(["lineTo", ...a]); },
      stroke() { calls.push(["stroke", this.strokeStyle, this.lineWidth]); },
      // A gradient fillStyle is an object with methods, and two fake
      // contexts build two distinct ones -- comparing them directly tests
      // object identity rather than what was painted. The stops are already
      // recorded by `addColorStop`, so the fill only needs a marker.
      fillRect(...a) {
        const fill =
          typeof this.fillStyle === "string" ? this.fillStyle : "<gradient>";
        calls.push(["fillRect", fill, ...a]);
      },
      createLinearGradient() { return gradient(calls); },
      createRadialGradient() { return gradient(calls); },
    };
  };
  const gradient = (calls) => ({
    addColorStop(...a) { calls.push(["stop", ...a]); },
  });

  for (const spec of bg.BACKGROUNDS) {
    const a = record();
    const b = record();
    bg.paintBackground(a, spec.id, 1080, 1350);
    bg.paintBackground(b, spec.id, 1080, 1350);
    assert.deepEqual(a.calls, b.calls, `${spec.id} painted differently twice`);
    assert.ok(a.calls.length > 0, `${spec.id} painted nothing`);
  }
});

test("an unknown id still paints a surface rather than nothing", () => {
  const calls = [];
  const ctx = {
    fillRect(...a) { calls.push(["fillRect", this.fillStyle, ...a]); },
    createLinearGradient: () => ({ addColorStop() {} }),
    createRadialGradient: () => ({ addColorStop() {} }),
    save() {}, restore() {}, beginPath() {}, moveTo() {}, lineTo() {}, stroke() {},
  };
  bg.paintBackground(ctx, "retired_marble", 100, 100);
  assert.ok(calls.length > 0, "an unknown background left a blank canvas");
});
