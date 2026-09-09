"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FONTS,
  MAX_QUOTES,
  MIN_QUOTES,
  QUOTE_LANGUAGES,
  fontById,
  isQuoteSettable,
  isRtl,
  normaliseHandle,
  type QuoteLanguage,
  type QuoteProject,
} from "@/lib/quotes";
import {
  BACKGROUNDS,
  DEFAULT_BACKGROUND,
  backgroundById,
  inkFor,
  paintBackground,
  type BackgroundId,
} from "@/lib/quote-backgrounds";
import { customerSafeError } from "@/lib/customer-errors";

/**
 * Quote Studio.
 *
 * Everything a slide is made of is local: the background comes from a fixed
 * library of numbers, the typography is CSS, and the export is canvas. Only
 * the quote *words* are written by a model. That split is what makes changing
 * a background instant and free, and what makes a saved project reopen
 * looking exactly as it did.
 *
 * The preview is DOM and the export is canvas, so the same layout is
 * expressed twice. They are kept honest by sharing the one thing that would
 * drift silently -- the ink palette, which both take from `inkFor` rather
 * than hard-coding per background.
 */

const RATIOS = [
  { id: "4:5" as const, label: "4:5", hint: "Feed carousel", w: 1080, h: 1350 },
  { id: "9:16" as const, label: "9:16", hint: "Stories & Reels", w: 1080, h: 1920 },
];

type Ratio = (typeof RATIOS)[number]["id"];

type Card = {
  id: string;
  text: string;
  kind: "quote" | "outro";
};

export function QuoteStudio({
  initialProject = null,
}: {
  initialProject?: QuoteProject | null;
}) {
  // ── the person ─────────────────────────────────────────────────────
  const snapshot = initialProject?.profileSnapshot;
  const [photo, setPhoto] = useState(snapshot?.photo ?? "");
  const [name, setName] = useState(snapshot?.displayName ?? "");
  const [username, setUsername] = useState(snapshot?.username ?? "");
  const [language, setLanguage] = useState<QuoteLanguage>(
    initialProject?.language ?? "en",
  );
  const [topic, setTopic] = useState(initialProject?.topic ?? "");

  // ── design ─────────────────────────────────────────────────────────
  const [fontId, setFontId] = useState(initialProject?.fontId ?? FONTS.en[0].id);
  const [ratioId, setRatioId] = useState<Ratio>(initialProject?.ratio ?? "4:5");
  // White by default, and whatever the project was saved with when reopened.
  const [backgroundId, setBackgroundId] = useState<BackgroundId>(
    initialProject?.backgroundId ?? DEFAULT_BACKGROUND,
  );

  // ── content ────────────────────────────────────────────────────────
  const [candidates, setCandidates] = useState<string[]>(
    initialProject?.quotes.map((q) => q.text) ?? [],
  );
  const [chosen, setChosen] = useState<string[]>(
    initialProject?.quotes.filter((q) => q.selected).map((q) => q.text) ?? [],
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [exporting, setExporting] = useState({ done: 0, total: 0 });

  const fileRef = useRef<HTMLInputElement>(null);

  const rtl = isRtl(language);
  const font = fontById(language, fontId);
  const background = backgroundById(backgroundId);
  const ink = useMemo(() => inkFor(background), [background]);
  const ratio = RATIOS.find((r) => r.id === ratioId) ?? RATIOS[0];
  const handle = normaliseHandle(username);

  // Switching language switches the typeface with it: an Arabic quote in a
  // Latin display face falls back mid-sentence and stops looking designed.
  useEffect(() => {
    if (!FONTS[language].some((f) => f.id === fontId)) {
      setFontId(FONTS[language][0].id);
    }
  }, [language, fontId]);

  const cards: Card[] = useMemo(
    () => [
      ...chosen.map((text, i) => ({ id: `q${i}`, text, kind: "quote" as const })),
      {
        id: "outro",
        text: rtl ? "تابعني للمزيد" : "Follow for more",
        kind: "outro" as const,
      },
    ],
    [chosen, rtl],
  );

  // ── photo ──────────────────────────────────────────────────────────

  const onPhoto = useCallback((file: File | null) => {
    if (!file) return;
    if (!file.type.startsWith("image/")) {
      setError("Choose an image file.");
      return;
    }
    // Matches the repository's stored-photo ceiling, so a photo that uploads
    // here cannot fail to save later.
    if (file.size > 1_000_000) {
      setError("That photo is over 1MB. Use a smaller one.");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      setPhoto(String(reader.result ?? ""));
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
        body: JSON.stringify({ topic, name, language, count: MIN_QUOTES + 1 }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Could not write quotes.");
      const list: string[] = data.quotes ?? [];
      setCandidates(list);
      setChosen(
        list.filter((q) => isQuoteSettable(q, language)).slice(0, MIN_QUOTES + 1),
      );
    } catch (err) {
      setError(customerSafeError((err as Error).message));
    } finally {
      setBusy(false);
    }
  }

  function toggleQuote(quote: string) {
    setChosen((prev) => {
      if (prev.includes(quote)) return prev.filter((q) => q !== quote);
      if (prev.length >= MAX_QUOTES) return prev;
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

  function editChosen(index: number, text: string) {
    setChosen((prev) => prev.map((q, i) => (i === index ? text : q)));
  }

  function move(from: number, to: number) {
    setChosen((prev) => {
      if (to < 0 || to >= prev.length) return prev;
      const next = [...prev];
      const [item] = next.splice(from, 1);
      next.splice(to, 0, item);
      return next;
    });
  }

  // ── saving ─────────────────────────────────────────────────────────

  async function saveProject() {
    if (chosen.length < MIN_QUOTES) {
      setError(`Choose at least ${MIN_QUOTES} quotes before saving.`);
      return;
    }
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const payload = {
        topic,
        language,
        fontId,
        ratio: ratioId,
        backgroundId,
        profile: { photo, displayName: name, username, preferredLanguage: language },
        quotes: chosen.map((text, position) => ({
          id: `q${position}`,
          text,
          selected: true,
          position,
        })),
      };
      const res = await fetch(
        initialProject ? `/api/quotes/projects/${initialProject.id}` : "/api/quotes/projects",
        {
          method: initialProject ? "PATCH" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(payload),
        },
      );
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Could not save.");
      setNotice("Saved.");
    } catch (err) {
      setError(customerSafeError((err as Error).message));
    } finally {
      setBusy(false);
    }
  }

  // ── export ─────────────────────────────────────────────────────────

  const exportCard = useCallback(
    async (card: Card, index: number) => {
      const canvas = document.createElement("canvas");
      canvas.width = ratio.w;
      canvas.height = ratio.h;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      paintBackground(ctx, backgroundId, canvas.width, canvas.height);
      await drawCard(ctx, {
        text: card.text,
        kind: card.kind,
        width: canvas.width,
        height: canvas.height,
        rtl,
        font,
        ink,
        name,
        handle,
        photo,
      });

      const blob: Blob | null = await new Promise((res) =>
        canvas.toBlob(res, "image/png"),
      );
      if (!blob) return;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `quote-${String(index + 1).padStart(2, "0")}.png`;
      a.click();
      URL.revokeObjectURL(url);
    },
    [ratio, backgroundId, rtl, font, ink, name, handle, photo],
  );

  async function exportAll() {
    setExporting({ done: 0, total: cards.length });
    for (let i = 0; i < cards.length; i++) {
      await exportCard(cards[i], i);
      setExporting({ done: i + 1, total: cards.length });
      // Browsers drop rapid successive downloads; a beat between them is the
      // difference between eight files and two.
      await new Promise((r) => setTimeout(r, 320));
    }
    setExporting({ done: 0, total: 0 });
  }

  // ── render ─────────────────────────────────────────────────────────

  return (
    <div className="studio quote-studio">
      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}
      {notice && <p className="notice qs-ok">{notice}</p>}

      {/* ── the person ───────────────────────────────────────────── */}
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
                  <span>Square works best. Under 1MB.</span>
                </span>
              )}
            </button>
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
                    onClick={() => setLanguage(l.code)}
                  >
                    {l.label}
                    <span className="seg-native">{l.native}</span>
                  </button>
                ))}
              </div>
            </div>

            <label className="field">
              <span>Topic or idea</span>
              <textarea
                value={topic}
                onChange={(e) => setTopic(e.target.value)}
                rows={2}
                maxLength={300}
                dir={rtl ? "rtl" : "ltr"}
                placeholder={
                  rtl ? "مثال: الانضباط أهم من الحماس" : "e.g. discipline beats motivation"
                }
              />
            </label>

            <button
              type="button"
              className="btn-generate"
              onClick={writeQuotes}
              disabled={busy || !topic.trim()}
            >
              {busy ? "Writing…" : "Write the quotes"}
            </button>
          </div>
        </div>
      </section>

      {/* ── quotes ───────────────────────────────────────────────── */}
      {candidates.length > 0 && (
        <section className="qs-panel">
          <h2 className="qs-h">
            The quotes
            <span className="qs-count">
              {chosen.length} of {MAX_QUOTES} chosen
            </span>
          </h2>
          <ul className="qs-quotes">
            {candidates.map((quote, i) => {
              const on = chosen.includes(quote);
              return (
                <li key={i} className={on ? "is-on" : ""}>
                  <button
                    type="button"
                    className="qs-check"
                    aria-pressed={on}
                    aria-label={on ? "Remove from deck" : "Add to deck"}
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
                  {!isQuoteSettable(quote, language) && (
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

      {/* ── design ───────────────────────────────────────────────── */}
      <section className="qs-panel">
        <h2 className="qs-h">Design</h2>

        <div className="opt-group">
          <span className="opt-label">Background</span>
          <div className="qs-bg-grid">
            {BACKGROUNDS.map((b) => {
              const bi = inkFor(b);
              return (
                <button
                  key={b.id}
                  type="button"
                  className={`qs-bg${backgroundId === b.id ? " is-on" : ""}`}
                  onClick={() => setBackgroundId(b.id)}
                  aria-pressed={backgroundId === b.id}
                  title={b.hint}
                >
                  <span className="qs-bg-chip" style={{ background: b.css }}>
                    <span className="qs-bg-aa" style={{ color: bi.ink }}>
                      {rtl ? "نص" : "Aa"}
                    </span>
                  </span>
                  <span className="qs-bg-label">{b.label}</span>
                </button>
              );
            })}
          </div>
          <p className="opt-hint">
            Local and instant — changing a background costs nothing and
            re-typesets the deck immediately. Text and photo contrast are set
            from the surface, so every option stays readable.
          </p>
        </div>

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
              </button>
            ))}
          </div>
        </div>

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

        {/* Stated rather than omitted: the deck really is free to render, and
            that is the point of a local background library. The only spend in
            this studio is the one call that writes the words. */}
        <div className="cost-panel">
          <div className="cost-row">
            <span>Backgrounds, typography &amp; export</span>
            <strong>$0.000</strong>
          </div>
          <div className="cost-row">
            <span>Writing the quotes (once per topic)</span>
            <strong>$0.002</strong>
          </div>
          <div className="cost-row cost-total">
            <span>Per carousel</span>
            <strong>$0.002</strong>
          </div>
        </div>
      </section>

      {/* ── deck ─────────────────────────────────────────────────── */}
      {chosen.length > 0 && (
        <section className="qs-panel">
          <h2 className="qs-h">
            The carousel
            <span className="qs-count">{cards.length} slides</span>
          </h2>

          {exporting.total > 0 && (
            <div className="qs-progress">
              <div
                className="qs-progress-bar"
                style={{
                  width: `${(exporting.done / Math.max(exporting.total, 1)) * 100}%`,
                }}
              />
              <span>
                Exporting {exporting.done} of {exporting.total}…
              </span>
            </div>
          )}

          <div className="qs-deck">
            {cards.map((card, i) => (
              <article
                key={card.id}
                className={`qs-slide-card${dragIndex === i ? " is-dragging" : ""}`}
                draggable={card.kind === "quote"}
                onDragStart={() => setDragIndex(i)}
                onDragEnd={() => setDragIndex(null)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => {
                  if (dragIndex !== null && card.kind === "quote") {
                    move(dragIndex, i);
                  }
                  setDragIndex(null);
                }}
              >
                <CardPreview
                  card={card}
                  ratio={ratio}
                  rtl={rtl}
                  font={font}
                  background={background}
                  ink={ink}
                  name={name}
                  handle={handle}
                  photo={photo}
                />

                <div className="qs-slide-tools">
                  <span className="qs-slide-n">{i + 1}</span>
                  <button
                    type="button"
                    onClick={() => move(i, i - 1)}
                    disabled={i === 0 || card.kind !== "quote"}
                    title="Move earlier"
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    onClick={() => move(i, i + 1)}
                    disabled={i >= chosen.length - 1 || card.kind !== "quote"}
                    title="Move later"
                  >
                    ↓
                  </button>
                  <button
                    type="button"
                    onClick={() => exportCard(card, i)}
                    title="Download this slide"
                  >
                    PNG
                  </button>
                </div>

                {card.kind === "quote" && (
                  <textarea
                    className="qs-slide-text"
                    value={card.text}
                    dir={rtl ? "rtl" : "ltr"}
                    rows={2}
                    onChange={(e) => editChosen(i, e.target.value)}
                    style={{ fontFamily: font.stack }}
                  />
                )}
              </article>
            ))}
          </div>

          <div className="qs-actions">
            <button
              type="button"
              className="btn-generate"
              onClick={exportAll}
              disabled={exporting.total > 0}
            >
              Download all {cards.length}
            </button>
            <button
              type="button"
              className="btn-ghost"
              onClick={saveProject}
              disabled={busy}
            >
              {busy ? "Saving…" : "Save project"}
            </button>
          </div>
        </section>
      )}
    </div>
  );
}

// ── preview ──────────────────────────────────────────────────────────

function CardPreview({
  card,
  ratio,
  rtl,
  font,
  background,
  ink,
  name,
  handle,
  photo,
}: {
  card: Card;
  ratio: { w: number; h: number };
  rtl: boolean;
  font: ReturnType<typeof fontById>;
  background: ReturnType<typeof backgroundById>;
  ink: ReturnType<typeof inkFor>;
  name: string;
  handle: string;
  photo: string;
}) {
  const isOutro = card.kind === "outro";
  return (
    <div
      className="qs-slide"
      dir={rtl ? "rtl" : "ltr"}
      style={{
        aspectRatio: `${ratio.w} / ${ratio.h}`,
        background: background.css,
      }}
    >
      <div className={`qs-slide-body${isOutro ? " is-outro" : ""}`}>
        {!isOutro && (
          <span className="qs-mark" style={{ color: ink.accent }} aria-hidden>
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
            color: ink.ink,
          }}
        >
          {card.text}
        </p>

        <span className="qs-rule" style={{ background: ink.rule }} />

        <div className="qs-sig">
          {photo && (
            // eslint-disable-next-line @next/next/no-img-element
            <img
              src={photo}
              alt=""
              className="qs-avatar"
              style={{ boxShadow: `0 0 0 2px ${ink.photoRing}` }}
            />
          )}
          <span className="qs-sig-text">
            {name && (
              <span className="qs-sig-name" style={{ color: ink.ink }}>
                {name}
              </span>
            )}
            {handle && (
              <span className="qs-sig-handle" style={{ color: ink.secondary }}>
                {handle}
              </span>
            )}
          </span>
        </div>
      </div>
    </div>
  );
}

// ── canvas export ────────────────────────────────────────────────────

/**
 * Wrap text to a width, in the language's own direction.
 *
 * Arabic is measured with the same `measureText` as English: the browser
 * shapes and joins the glyphs before measuring, so a character count would be
 * wrong but a measured width is right. Direction comes from the canvas
 * `direction` property, never from reversing the string -- reversing breaks
 * the shaping and produces disconnected letters.
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

async function drawCard(
  ctx: CanvasRenderingContext2D,
  opts: {
    text: string;
    kind: "quote" | "outro";
    width: number;
    height: number;
    rtl: boolean;
    font: ReturnType<typeof fontById>;
    ink: ReturnType<typeof inkFor>;
    name: string;
    handle: string;
    photo: string;
  },
) {
  const { text, kind, width, height, rtl, font, ink, name, handle, photo } = opts;

  const pad = Math.round(width * 0.11);
  const maxWidth = width - pad * 2;
  ctx.direction = rtl ? "rtl" : "ltr";
  ctx.textAlign = rtl ? "right" : "left";
  const x = rtl ? width - pad : pad;

  // Fit by stepping down until the quote fits its band. Starting large and
  // shrinking is what keeps a short quote big, which is the whole visual
  // point of a quote card.
  const band = height * (kind === "outro" ? 0.26 : 0.44);
  const min = Math.round(width * 0.04);
  let size = Math.round(width * (kind === "outro" ? 0.082 : 0.094));
  let lines: string[] = [];

  for (; size > min; size -= 2) {
    ctx.font = `${font.weight} ${size}px ${font.stack}`;
    lines = layoutLines(ctx, text, maxWidth);
    if (lines.length * size * font.lineHeight <= band) break;
  }

  ctx.font = `${font.weight} ${size}px ${font.stack}`;
  const lineHeight = size * font.lineHeight;
  const sigBlock = Math.round(width * 0.16);
  let y = height - pad - sigBlock - lines.length * lineHeight;

  if (kind === "quote") {
    ctx.fillStyle = ink.accent;
    ctx.font = `${font.weight} ${Math.round(size * 1.8)}px ${font.stack}`;
    ctx.fillText(rtl ? "”" : "“", x, y - Math.round(size * 0.3));
    ctx.font = `${font.weight} ${size}px ${font.stack}`;
  }

  ctx.fillStyle = ink.ink;
  for (const line of lines) {
    y += lineHeight;
    ctx.fillText(line, x, y);
  }

  // Hairline above the signature.
  const ruleY = height - pad - Math.round(width * 0.115);
  ctx.fillStyle = ink.rule;
  ctx.fillRect(rtl ? width - pad - maxWidth : pad, ruleY, maxWidth, 1);

  // Signature: avatar, name, handle.
  const avatar = Math.round(width * 0.075);
  const baseY = height - pad - avatar / 2;
  let textX = x;

  if (photo) {
    const img = new Image();
    img.src = photo;
    try {
      await new Promise((res, rej) => {
        img.onload = res;
        img.onerror = rej;
      });
      const cx = rtl ? width - pad - avatar / 2 : pad + avatar / 2;
      ctx.save();
      ctx.beginPath();
      ctx.arc(cx, baseY, avatar / 2, 0, Math.PI * 2);
      ctx.closePath();
      ctx.clip();
      const scale = Math.max(avatar / img.width, avatar / img.height);
      ctx.drawImage(
        img,
        cx - (img.width * scale) / 2,
        baseY - (img.height * scale) / 2,
        img.width * scale,
        img.height * scale,
      );
      ctx.restore();
      // The ring keeps a light photo from dissolving into a light surface.
      ctx.strokeStyle = ink.photoRing;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, baseY, avatar / 2, 0, Math.PI * 2);
      ctx.stroke();
      textX = rtl ? x - avatar - Math.round(width * 0.03)
                  : x + avatar + Math.round(width * 0.03);
    } catch {
      // An unreadable photo must not lose the whole export; the name and
      // handle still identify the card.
    }
  }

  if (name) {
    ctx.fillStyle = ink.ink;
    ctx.font = `700 ${Math.round(width * 0.033)}px ${font.stack}`;
    ctx.fillText(name, textX, baseY - Math.round(width * 0.004));
  }
  if (handle) {
    ctx.fillStyle = ink.secondary;
    ctx.font = `500 ${Math.round(width * 0.028)}px ${font.stack}`;
    ctx.fillText(handle, textX, baseY + Math.round(width * 0.036));
  }
}
