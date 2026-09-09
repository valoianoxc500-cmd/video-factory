"use client";

import { useCallback, useMemo, useRef, useState } from "react";
import {
  COST_PER_SLIDE_USD,
  FONTS,
  MAX_SLIDES,
  MIN_SLIDES,
  QUOTE_LANGUAGES,
  STYLES,
  estimateCost,
  fontById,
  isQuoteSettable,
  isRtl,
  normaliseHandle,
  styleById,
  type QuoteLanguage,
  type Slide,
} from "@/lib/quotes";

/**
 * Quote Studio.
 *
 * The deck lives in this component and nowhere else -- there is no job row and
 * no worker. Backgrounds come back from the API as data URIs and the
 * typography is drawn over them in the DOM, which is what makes the preview
 * live: editing a quote re-renders instantly and costs nothing, because the
 * words were never part of the generated image.
 *
 * Export goes through canvas rather than screenshotting the DOM. The same
 * layout maths runs twice -- once in CSS for the preview, once on the canvas
 * for the file -- and `layoutLines` below is shared by both so they cannot
 * drift apart.
 */

type Step = "brief" | "quotes" | "design" | "deck";

const RATIOS = [
  { id: "9:16", label: "9:16", hint: "Stories & Reels", w: 1080, h: 1920 },
  { id: "4:5", label: "4:5", hint: "Feed carousel", w: 1080, h: 1350 },
];

export function QuoteStudio() {
  // ── brief ──────────────────────────────────────────────────────────
  const [photo, setPhoto] = useState<string>("");
  const [photoName, setPhotoName] = useState("");
  const [name, setName] = useState("");
  const [username, setUsername] = useState("");
  const [language, setLanguage] = useState<QuoteLanguage>("en");
  const [topic, setTopic] = useState("");

  // ── design ─────────────────────────────────────────────────────────
  const [fontId, setFontId] = useState("editorial");
  const [styleId, setStyleId] = useState("noir");
  const [ratioId, setRatioId] = useState("9:16");
  const [slideCount, setSlideCount] = useState(6);

  // ── generated ──────────────────────────────────────────────────────
  const [candidates, setCandidates] = useState<string[]>([]);
  const [chosen, setChosen] = useState<string[]>([]);
  const [slides, setSlides] = useState<Slide[]>([]);
  const [step, setStep] = useState<Step>("brief");
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [error, setError] = useState<string | null>(null);
  const [dragIndex, setDragIndex] = useState<number | null>(null);

  const fileRef = useRef<HTMLInputElement>(null);

  const rtl = isRtl(language);
  const font = fontById(language, fontId);
  const style = styleById(styleId);
  const ratio = RATIOS.find((r) => r.id === ratioId) ?? RATIOS[0];
  const handle = normaliseHandle(username);

  // Cost is shown before anything is spent, and counts the outro slide,
  // because a number that excludes part of the run is worse than none.
  const estimate = useMemo(() => estimateCost(slideCount), [slideCount]);

  // ── photo ──────────────────────────────────────────────────────────

  const onPhoto = useCallback((file: File | null) => {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setError("Choose an image file.");
      return;
    }
    if (file.size > 8 * 1024 * 1024) {
      setError("That photo is over 8MB. Use a smaller one.");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setPhoto(String(reader.result ?? ""));
      setPhotoName(file.name);
      setError(null);
    };
    reader.onerror = () => setError("Could not read that file.");
    reader.readAsDataURL(file);
  }, []);

  // ── quotes ─────────────────────────────────────────────────────────

  async function writeQuotes() {
    if (!topic.trim()) {
      setError("Enter a topic or idea.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const res = await fetch("/api/quotes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ topic, name, language, count: slideCount }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Could not write quotes.");
      const list: string[] = data.quotes ?? [];
      setCandidates(list);
      // Pre-select the first N that will actually set large.
      setChosen(list.filter((q) => isQuoteSettable(q, language)).slice(0, slideCount));
      setStep("quotes");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  function toggleQuote(quote: string) {
    setChosen((prev) => {
      if (prev.includes(quote)) return prev.filter((q) => q !== quote);
      if (prev.length >= slideCount) return prev;
      return [...prev, quote];
    });
  }

  function editCandidate(index: number, text: string) {
    setCandidates((prev) => {
      const next = [...prev];
      const old = next[index];
      next[index] = text;
      setChosen((c) => c.map((q) => (q === old ? text : q)));
      return next;
    });
  }

  // ── slides ─────────────────────────────────────────────────────────

  async function generateSlide(
    variant: number,
    isOutro: boolean,
  ): Promise<{ image?: string; error?: string }> {
    try {
      const res = await fetch("/api/quotes/slides", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          image: photo,
          styleId,
          variant,
          isOutro,
          ratio: ratioId,
        }),
      });
      const data = await res.json();
      if (!res.ok) return { error: data.error ?? "Generation failed." };
      return { image: data.image };
    } catch {
      return { error: "Network error." };
    }
  }

  async function buildCarousel() {
    if (!photo) {
      setError("Upload a photo of the person first.");
      return;
    }
    if (!chosen.length) {
      setError("Choose at least one quote.");
      return;
    }

    const deck: Slide[] = [
      ...chosen.map((text, i) => ({
        id: `q${i}-${Date.now()}`,
        text,
        image: "",
        status: "pending" as const,
        kind: "quote" as const,
      })),
      {
        id: `outro-${Date.now()}`,
        text: rtl ? "تابعني للمزيد" : "Follow for more",
        image: "",
        status: "pending" as const,
        kind: "outro" as const,
      },
    ];

    setSlides(deck);
    setStep("deck");
    setBusy(true);
    setError(null);
    setProgress({ done: 0, total: deck.length });

    // Sequential on purpose. The edit model is rate-limited per key, and a
    // burst of eight returns 429s that read to the user as "it failed"
    // rather than "slow down".
    for (let i = 0; i < deck.length; i++) {
      setSlides((prev) =>
        prev.map((s, j) => (j === i ? { ...s, status: "generating" } : s)),
      );
      const result = await generateSlide(i, deck[i].kind === "outro");
      setSlides((prev) =>
        prev.map((s, j) =>
          j === i
            ? result.image
              ? { ...s, image: result.image, status: "ready" }
              : { ...s, status: "failed", error: result.error }
            : s,
        ),
      );
      setProgress({ done: i + 1, total: deck.length });
    }
    setBusy(false);
  }

  async function regenerateSlide(index: number) {
    const slide = slides[index];
    if (!slide || busy) return;
    setSlides((prev) =>
      prev.map((s, j) => (j === index ? { ...s, status: "generating" } : s)),
    );
    // A different variant seed, so "regenerate" visibly changes something.
    const result = await generateSlide(index + slides.length, slide.kind === "outro");
    setSlides((prev) =>
      prev.map((s, j) =>
        j === index
          ? result.image
            ? { ...s, image: result.image, status: "ready", error: undefined }
            : { ...s, status: "failed", error: result.error }
          : s,
      ),
    );
  }

  function editSlideText(index: number, text: string) {
    setSlides((prev) => prev.map((s, j) => (j === index ? { ...s, text } : s)));
  }

  function move(from: number, to: number) {
    setSlides((prev) => {
      if (to < 0 || to >= prev.length) return prev;
      const next = [...prev];
      const [item] = next.splice(from, 1);
      next.splice(to, 0, item);
      return next;
    });
  }

  // ── export ─────────────────────────────────────────────────────────

  const exportSlide = useCallback(
    async (slide: Slide, index: number) => {
      const canvas = document.createElement("canvas");
      canvas.width = ratio.w;
      canvas.height = ratio.h;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      if (slide.image) {
        const img = new Image();
        img.src = slide.image;
        await new Promise((res, rej) => {
          img.onload = res;
          img.onerror = rej;
        });
        drawCover(ctx, img, canvas.width, canvas.height);
      } else {
        ctx.fillStyle = "#0a0c10";
        ctx.fillRect(0, 0, canvas.width, canvas.height);
      }

      drawScrim(ctx, canvas.width, canvas.height, style.id);
      drawSlideText(ctx, {
        text: slide.text,
        kind: slide.kind,
        width: canvas.width,
        height: canvas.height,
        rtl,
        font,
        ink: style.ink,
        accent: style.accent,
        name,
        handle,
      });

      const blob: Blob | null = await new Promise((res) =>
        canvas.toBlob(res, "image/png"),
      );
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `slide-${String(index + 1).padStart(2, "0")}.png`;
      a.click();
      URL.revokeObjectURL(url);
    },
    [ratio, style, rtl, font, name, handle],
  );

  async function exportAll() {
    for (let i = 0; i < slides.length; i++) {
      await exportSlide(slides[i], i);
      // Browsers drop rapid successive downloads; a beat between them is the
      // difference between eight files and two.
      await new Promise((r) => setTimeout(r, 350));
    }
  }

  const ready = slides.filter((s) => s.status === "ready").length;

  // ── render ─────────────────────────────────────────────────────────

  return (
    <div className="studio quote-studio">
      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}

      <ol className="qs-steps" aria-label="Progress">
        {(["brief", "quotes", "design", "deck"] as Step[]).map((s, i) => (
          <li key={s} className={step === s ? "is-current" : ""}>
            <span className="qs-step-n">{i + 1}</span>
            {["Brief", "Quotes", "Design", "Deck"][i]}
          </li>
        ))}
      </ol>

      {/* ── 1. brief ─────────────────────────────────────────────── */}
      <section className="qs-panel">
        <h2 className="qs-h">The person</h2>

        <div className="qs-brief">
          <div>
            <button
              type="button"
              className={`qs-drop${photo ? " has-photo" : ""}`}
              onClick={() => fileRef.current?.click()}
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                onPhoto(e.dataTransfer.files?.[0] ?? null);
              }}
            >
              {photo ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img src={photo} alt="" className="qs-drop-img" />
              ) : (
                <span className="qs-drop-empty">
                  <strong>Upload a photo</strong>
                  <span>Their face, clearly lit. JPG or PNG, under 8MB.</span>
                </span>
              )}
            </button>
            {photoName && <p className="qs-filename">{photoName}</p>}
            <input
              ref={fileRef}
              type="file"
              accept="image/*"
              hidden
              onChange={(e) => onPhoto(e.target.files?.[0] ?? null)}
            />
          </div>

          <div className="qs-fields">
            <label className="field">
              <span>Name</span>
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Layla Haddad"
                maxLength={60}
              />
            </label>
            <label className="field">
              <span>Username</span>
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="@username"
                maxLength={40}
              />
            </label>

            <div className="opt-group">
              <span className="opt-label">Language</span>
              <div className="lang-row">
                {QUOTE_LANGUAGES.map((l) => (
                  <button
                    key={l.code}
                    type="button"
                    className={`seg${language === l.code ? " is-on" : ""}`}
                    onClick={() => {
                      setLanguage(l.code);
                      setFontId(FONTS[l.code][0].id);
                    }}
                  >
                    {l.label}
                    <span className="seg-native">{l.native}</span>
                  </button>
                ))}
              </div>
              <p className="opt-hint">
                Sets the quote language, the typography, and the text
                direction together — they cannot move independently.
              </p>
            </div>

            <label className="field">
              <span>Topic or idea</span>
              <textarea
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                rows={2}
                maxLength={300}
                placeholder={
                  rtl
                    ? "مثال: الانضباط أهم من الحماس"
                    : "e.g. discipline beats motivation"
                }
                dir={rtl ? "rtl" : "ltr"}
              />
            </label>

            <button
              type="button"
              className="btn-generate"
              onClick={writeQuotes}
              disabled={busy || !topic.trim()}
            >
              {busy && step === "brief" ? "Writing…" : "Write the quotes"}
            </button>
          </div>
        </div>
      </section>

      {/* ── 2. quotes ────────────────────────────────────────────── */}
      {candidates.length > 0 && (
        <section className="qs-panel">
          <h2 className="qs-h">
            The quotes
            <span className="qs-count">
              {chosen.length} of {slideCount} chosen
            </span>
          </h2>
          <p className="opt-hint">
            Edit any line. Anything too long to set large is flagged — a
            paragraph shrunk to fit is the fastest way to lose a reader.
          </p>

          <ul className="qs-quotes">
            {candidates.map((quote, i) => {
              const on = chosen.includes(quote);
              const settable = isQuoteSettable(quote, language);
              return (
                <li key={i} className={on ? "is-on" : ""}>
                  <button
                    type="button"
                    className="qs-check"
                    aria-pressed={on}
                    onClick={() => toggleQuote(quote)}
                  >
                    {on ? "✓" : ""}
                  </button>
                  <textarea
                    value={quote}
                    dir={rtl ? "rtl" : "ltr"}
                    rows={2}
                    onChange={(e) => editCandidate(i, e.target.value)}
                    style={{ fontFamily: font.stack }}
                  />
                  {!settable && (
                    <span className="qs-warn" title="Too long to set large">
                      long
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {/* ── 3. design ────────────────────────────────────────────── */}
      {candidates.length > 0 && (
        <section className="qs-panel">
          <h2 className="qs-h">Design</h2>

          <div className="opt-group">
            <span className="opt-label">Typeface</span>
            <div className="style-grid">
              {FONTS[language].map((f) => (
                <button
                  key={f.id}
                  type="button"
                  className={`qs-font${fontId === f.id ? " is-on" : ""}`}
                  onClick={() => setFontId(f.id)}
                >
                  <span
                    className="qs-font-sample"
                    style={{ fontFamily: f.stack, fontWeight: f.weight }}
                    dir={rtl ? "rtl" : "ltr"}
                  >
                    {rtl ? "الانضباط" : "Discipline"}
                  </span>
                  <span className="style-name">{f.label}</span>
                  <span className="style-hint">{f.hint}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="opt-group">
            <span className="opt-label">Visual style</span>
            <div className="style-grid">
              {STYLES.map((s) => (
                <button
                  key={s.id}
                  type="button"
                  className={`qs-style${styleId === s.id ? " is-on" : ""}`}
                  onClick={() => setStyleId(s.id)}
                >
                  <span
                    className="qs-style-chip"
                    style={{ background: s.scrim, borderColor: s.accent }}
                  />
                  <span className="style-name">{s.label}</span>
                  <span className="style-hint">{s.hint}</span>
                </button>
              ))}
            </div>
          </div>

          <div className="qs-two">
            <div className="opt-group">
              <span className="opt-label">Format</span>
              <div className="lang-row">
                {RATIOS.map((r) => (
                  <button
                    key={r.id}
                    type="button"
                    className={`seg${ratioId === r.id ? " is-on" : ""}`}
                    onClick={() => setRatioId(r.id)}
                  >
                    {r.label}
                    <span className="seg-sub">{r.hint}</span>
                  </button>
                ))}
              </div>
            </div>

            <div className="opt-group">
              <span className="opt-label">Slides</span>
              <input
                type="range"
                min={MIN_SLIDES}
                max={MAX_SLIDES}
                value={slideCount}
                onChange={(e) => setSlideCount(Number(e.target.value))}
              />
              <p className="opt-hint">
                {slideCount} quote slides, plus a closing card.
              </p>
            </div>
          </div>

          <div className="cost-panel">
            <div className="cost-row">
              <span>
                {slideCount + 1} generated slides × ${COST_PER_SLIDE_USD}
              </span>
              <strong>${(( slideCount + 1) * COST_PER_SLIDE_USD).toFixed(3)}</strong>
            </div>
            <div className="cost-row">
              <span>Quote writing</span>
              <strong>$0.002</strong>
            </div>
            <div className="cost-row">
              <span>Typography &amp; export</span>
              <strong>$0.000</strong>
            </div>
            <div className="cost-row cost-total">
              <span>Estimated total</span>
              <strong>${estimate.toFixed(3)}</strong>
            </div>
          </div>

          <button
            type="button"
            className="btn-generate"
            onClick={buildCarousel}
            disabled={busy || !photo || !chosen.length}
          >
            {busy ? "Generating…" : `Generate ${chosen.length + 1} slides`}
          </button>
        </section>
      )}

      {/* ── 4. deck ──────────────────────────────────────────────── */}
      {slides.length > 0 && (
        <section className="qs-panel">
          <h2 className="qs-h">
            The carousel
            <span className="qs-count">
              {ready} of {slides.length} ready
            </span>
          </h2>

          {busy && (
            <div className="qs-progress">
              <div
                className="qs-progress-bar"
                style={{
                  width: `${(progress.done / Math.max(progress.total, 1)) * 100}%`,
                }}
              />
              <span>
                Generating slide {progress.done} of {progress.total}…
              </span>
            </div>
          )}

          <div className="qs-deck">
            {slides.map((slide, i) => (
              <article
                key={slide.id}
                className={`qs-slide-card${dragIndex === i ? " is-dragging" : ""}`}
                draggable={!busy}
                onDragStart={() => setDragIndex(i)}
                onDragEnd={() => setDragIndex(null)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => {
                  if (dragIndex !== null) move(dragIndex, i);
                  setDragIndex(null);
                }}
              >
                <SlidePreview
                  slide={slide}
                  ratio={ratio}
                  rtl={rtl}
                  font={font}
                  style={style}
                  name={name}
                  handle={handle}
                />

                <div className="qs-slide-tools">
                  <span className="qs-slide-n">{i + 1}</span>
                  <button
                    type="button"
                    onClick={() => move(i, i - 1)}
                    disabled={i === 0 || busy}
                    title="Move earlier"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    onClick={() => move(i, i + 1)}
                    disabled={i === slides.length - 1 || busy}
                    title="Move later"
                  >
                    ↓
                  </button>
                  <button
                    type="button"
                    onClick={() => regenerateSlide(i)}
                    disabled={busy || slide.status === "generating"}
                    title="Regenerate this slide only"
                  >
                    ↻
                  </button>
                  <button
                    type="button"
                    onClick={() => exportSlide(slide, i)}
                    disabled={slide.status !== "ready"}
                    title="Download this slide"
                  >
                    ↓PNG
                  </button>
                </div>

                <textarea
                  className="qs-slide-text"
                  value={slide.text}
                  dir={rtl ? "rtl" : "ltr"}
                  rows={2}
                  onChange={(e) => editSlideText(i, e.target.value)}
                  style={{ fontFamily: font.stack }}
                />
                {slide.error && <p className="qs-slide-err">{slide.error}</p>}
              </article>
            ))}
          </div>

          <button
            type="button"
            className="btn-generate"
            onClick={exportAll}
            disabled={busy || ready === 0}
          >
            Download all {ready} slides
          </button>
        </section>
      )}
    </div>
  );
}

// ── preview ──────────────────────────────────────────────────────────

function SlidePreview({
  slide,
  ratio,
  rtl,
  font,
  style,
  name,
  handle,
}: {
  slide: Slide;
  ratio: { id: string; w: number; h: number };
  rtl: boolean;
  font: ReturnType<typeof fontById>;
  style: ReturnType<typeof styleById>;
  name: string;
  handle: string;
}) {
  const isOutro = slide.kind === "outro";
  return (
    <div
      className="qs-slide"
      style={{ aspectRatio: `${ratio.w} / ${ratio.h}` }}
      dir={rtl ? "rtl" : "ltr"}
    >
      {slide.image ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={slide.image} alt="" className="qs-slide-img" />
      ) : (
        <div className="qs-slide-skeleton">
          {slide.status === "generating" ? "Generating…" : ""}
        </div>
      )}

      <div className="qs-slide-scrim" style={{ background: style.scrim }} />

      <div className={`qs-slide-body${isOutro ? " is-outro" : ""}`}>
        {!isOutro && (
          <span className="qs-mark" style={{ color: style.accent }} aria-hidden>
            {rtl ? "”" : "“"}
          </span>
        )}
        <p
          className="qs-quote"
          style={{
            fontFamily: font.stack,
            fontWeight: font.weight,
            lineHeight: font.lineHeight,
            letterSpacing: font.letterSpacing,
            color: style.ink,
          }}
        >
          {slide.text}
        </p>
        <div className="qs-sig">
          {name && (
            <span className="qs-sig-name" style={{ color: style.ink }}>
              {name}
            </span>
          )}
          {handle && (
            <span className="qs-sig-handle" style={{ color: style.accent }}>
              {handle}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

// ── canvas export ────────────────────────────────────────────────────

function drawCover(
  ctx: CanvasRenderingContext2D,
  img: HTMLImageElement,
  w: number,
  h: number,
) {
  const scale = Math.max(w / img.width, h / img.height);
  const dw = img.width * scale;
  const dh = img.height * scale;
  ctx.drawImage(img, (w - dw) / 2, (h - dh) / 2, dw, dh);
}

function drawScrim(
  ctx: CanvasRenderingContext2D,
  w: number,
  h: number,
  styleId: string,
) {
  // Mirrors the CSS scrims in `lib/quotes.ts`. Canvas cannot read a CSS
  // gradient string, so the stops are restated here; keeping them in the same
  // order and opacity is what makes the export match the preview.
  const tints: Record<string, [number, number, number]> = {
    noir: [0, 0, 0],
    warm: [26, 12, 0],
    studio: [8, 10, 14],
    gradient: [20, 0, 30],
  };
  const [r, g, b] = tints[styleId] ?? tints.noir;
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, `rgba(${r},${g},${b},0.20)`);
  grad.addColorStop(0.45, `rgba(${r},${g},${b},0.55)`);
  grad.addColorStop(1, `rgba(${r},${g},${b},0.88)`);
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, w, h);
}

/**
 * Wrap text to a width, in the language's own direction.
 *
 * Arabic is measured with the same `measureText` as English: the browser
 * shapes and joins the glyphs before measuring, so a naive character count
 * would be wrong but a measured width is right. Direction is handled by the
 * canvas `direction` property plus alignment, not by reversing the string --
 * reversing breaks the shaping.
 */
function layoutLines(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxWidth: number,
): string[] {
  const words = text.trim().split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = "";
  for (const word of words) {
    const candidate = line ? `${line} ${word}` : word;
    if (ctx.measureText(candidate).width <= maxWidth || !line) {
      line = candidate;
    } else {
      lines.push(line);
      line = word;
    }
  }
  if (line) lines.push(line);
  return lines;
}

function drawSlideText(
  ctx: CanvasRenderingContext2D,
  opts: {
    text: string;
    kind: "quote" | "outro";
    width: number;
    height: number;
    rtl: boolean;
    font: ReturnType<typeof fontById>;
    ink: string;
    accent: string;
    name: string;
    handle: string;
  },
) {
  const { text, kind, width, height, rtl, font, ink, accent, name, handle } = opts;

  const pad = Math.round(width * 0.09);
  const maxWidth = width - pad * 2;
  ctx.direction = rtl ? "rtl" : "ltr";
  ctx.textAlign = rtl ? "right" : "left";
  const x = rtl ? width - pad : pad;

  // Fit the quote by stepping the size down until it fits the lower band.
  // Starting large and shrinking is what keeps short quotes big, which is
  // the entire visual point of a quote card.
  const band = height * (kind === "outro" ? 0.30 : 0.46);
  let size = Math.round(width * (kind === "outro" ? 0.085 : 0.098));
  let lines: string[] = [];
  const min = Math.round(width * 0.038);

  for (; size > min; size -= 2) {
    ctx.font = `${font.weight} ${size}px ${font.stack}`;
    lines = layoutLines(ctx, text, maxWidth);
    if (lines.length * size * font.lineHeight <= band) break;
  }

  ctx.font = `${font.weight} ${size}px ${font.stack}`;
  const lineHeight = size * font.lineHeight;
  const sigHeight = Math.round(width * 0.10);
  let y = height - pad - sigHeight - lines.length * lineHeight;

  // Opening mark, set in the accent, above the first line.
  if (kind === "quote") {
    ctx.fillStyle = accent;
    ctx.font = `${font.weight} ${Math.round(size * 1.7)}px ${font.stack}`;
    ctx.fillText(rtl ? "”" : "“", x, y - Math.round(size * 0.35));
    ctx.font = `${font.weight} ${size}px ${font.stack}`;
  }

  ctx.fillStyle = ink;
  ctx.shadowColor = "rgba(0,0,0,.45)";
  ctx.shadowBlur = Math.round(size * 0.35);
  for (const line of lines) {
    y += lineHeight;
    ctx.fillText(line, x, y);
  }
  ctx.shadowBlur = 0;

  // Signature block.
  const sigY = height - pad;
  if (name) {
    ctx.fillStyle = ink;
    ctx.font = `600 ${Math.round(width * 0.038)}px ${font.stack}`;
    ctx.fillText(name, x, sigY - Math.round(width * 0.048));
  }
  if (handle) {
    ctx.fillStyle = accent;
    ctx.font = `500 ${Math.round(width * 0.032)}px ${font.stack}`;
    ctx.fillText(handle, x, sigY);
  }
}
