"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  FONTS,
  QUOTE_LANGUAGES,
  fontById,
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
import {
  CAROUSEL_LANGUAGES,
  IDEA_EXAMPLES,
  SLIDE_COUNTS,
  TONES,
  cardsFromSlides,
  makeSlide,
  normaliseSlides,
  slidesFromCards,
  slideFits,
  withRoles,
  type CarouselLanguage,
  type Slide,
  type Tone,
} from "@/lib/carousel";
import { CAPTION_LIMITS } from "@/lib/carousel";
import {
  adaptCaption,
  canPublish,
  connectionStates,
  coverageNote,
  type ConnectionState,
} from "@/lib/carousel-publish";
import { customerSafeError } from "@/lib/customer-errors";

/**
 * AI Carousel Studio — the Quote Studio, rebuilt around one idea.
 *
 * The old studio asked for a topic and returned N independent aphorisms; the
 * user picked their favourites. That is a quote generator. A carousel is one
 * argument split across slides, so the model is asked for the whole thing at
 * once and the slides arrive already connected.
 *
 * Two behaviours follow from that and are worth stating, because they are the
 * difference between this and a per-slide generator:
 *
 *   - regenerating one slide sends its *neighbours* as context, so the
 *     replacement fits where it landed instead of drifting off on its own
 *   - roles are positional, so dragging the closing slide to the front makes
 *     it the hook rather than leaving a deck with two endings
 *
 * Rendering is unchanged: local, deterministic, free, and the same canvas
 * export the studio always had.
 */

const RATIOS = [
  { id: "4:5" as const, label: "4:5", hint: "Feed", w: 1080, h: 1350 },
  { id: "1:1" as const, label: "1:1", hint: "Square", w: 1080, h: 1080 },
  { id: "9:16" as const, label: "9:16", hint: "Stories", w: 1080, h: 1920 },
];
type Ratio = (typeof RATIOS)[number]["id"];

export function QuoteStudio({
  initialProject = null,
}: {
  initialProject?: QuoteProject | null;
}) {
  const snapshot = initialProject?.profileSnapshot;

  // ── identity (unchanged from the old studio) ───────────────────────
  const [photo, setPhoto] = useState(snapshot?.photo ?? "");
  const [name, setName] = useState(snapshot?.displayName ?? "");
  const [username, setUsername] = useState(snapshot?.username ?? "");

  // ── the idea ───────────────────────────────────────────────────────
  const [idea, setIdea] = useState(initialProject?.topic ?? "");
  const [language, setLanguage] = useState<CarouselLanguage>(
    initialProject?.language ?? "auto",
  );
  const [slideCount, setSlideCount] = useState<string>("auto");
  const [tone, setTone] = useState<Tone>("educational");

  // ── the carousel ───────────────────────────────────────────────────
  const projectBackground = initialProject?.backgroundId ?? DEFAULT_BACKGROUND;
  const [slides, setSlides] = useState<Slide[]>(
    initialProject ? slidesFromCards(initialProject.quotes, projectBackground) : [],
  );
  // The language actually written in, which "Auto" only resolves at generation.
  const [written, setWritten] = useState<QuoteLanguage>(
    initialProject?.language ?? "en",
  );
  const [fontId, setFontId] = useState(initialProject?.fontId ?? FONTS.en[0].id);
  const [ratioId, setRatioId] = useState<Ratio>(initialProject?.ratio ?? "4:5");

  const [busy, setBusy] = useState(false);
  const [regenerating, setRegenerating] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [exporting, setExporting] = useState({ done: 0, total: 0 });


  /**
   * Publish: render, upload, then post. In that order, and only in that order.
   *
   * If any slide fails to upload the providers are never called at all --
   * publishing a carousel with a missing slide is worse than not publishing.
   */
  async function publishNow() {
    if (!initialProject) {
      setError("Save the project before publishing.");
      return;
    }
    if (!chosen.length) return;

    setBusy(true);
    setError(null);
    setPublishResults([]);
    try {
      setPublishState("Preparing carousel…");
      const blobs: Blob[] = [];
      for (const slide of slides) {
        const blob = await renderSlide(slide);
        if (!blob) throw new Error("A slide could not be rendered.");
        blobs.push(blob);
      }

      setPublishState("Uploading slides…");
      const signRes = await fetch("/api/quotes/carousel/upload", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          projectId: initialProject.id,
          count: blobs.length,
          contentType: "image/png",
        }),
      });
      const signed = await signRes.json();
      if (!signRes.ok) throw new Error(signed.error ?? "Could not prepare the upload.");

      const targets = signed.slides as {
        objectPath: string;
        uploadUrl: string;
        headers: Record<string, string>;
      }[];

      // Sequential, so slide order is also upload order and a failure stops
      // before the rest are spent.
      for (let i = 0; i < blobs.length; i++) {
        const put = await fetch(targets[i].uploadUrl, {
          method: "PUT",
          headers: targets[i].headers,
          body: blobs[i],
        });
        if (!put.ok) throw new Error("A slide could not be uploaded.");
      }

      setPublishState("Publishing…");
      const publishRes = await fetch("/api/quotes/publish", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          confirmed: true,
          projectId: initialProject.id,
          slidePaths: targets.map((t) => t.objectPath),
          platforms: chosen,
          caption,
          captions: captionFor,
        }),
      });
      const data = await publishRes.json();
      if (!publishRes.ok) throw new Error(data.error ?? "Could not publish.");
      setPublishResults(data.results ?? []);
      setConfirming(false);
    } catch (err) {
      setError(customerSafeError((err as Error).message));
    } finally {
      setPublishState("");
      setBusy(false);
    }
  }

  // ── publishing ─────────────────────────────────────────────────────
  const [connections, setConnections] = useState<ConnectionState[]>([]);
  const [chosen, setChosen] = useState<string[]>([]);
  const [caption, setCaption] = useState("");
  const [confirming, setConfirming] = useState(false);
  // Per-platform captions, derived from the master but editable. Empty
  // means "use the master", so editing one does not freeze the others.
  const [captions, setCaptions] = useState<Record<string, string>>({});
  // What to do when a carousel is longer than X can carry. Nothing is
  // truncated until the user says so.
  const [xOverflow, setXOverflow] = useState<"undecided" | "trim">("undecided");
  const [publishState, setPublishState] = useState("");
  const [publishResults, setPublishResults] = useState<
    { platform: string; status: string; message: string }[]
  >([]);

  const fileRef = useRef<HTMLInputElement>(null);

  const rtl = isRtl(written);
  const font = fontById(written, fontId);
  const ratio = RATIOS.find((r) => r.id === ratioId) ?? RATIOS[0];
  const handle = normaliseHandle(username);
  const hasCarousel = slides.length > 0;

  useEffect(() => {
    if (!FONTS[written].some((f) => f.id === fontId)) {
      setFontId(FONTS[written][0].id);
    }
  }, [written, fontId]);

  // Real connection state, read once the carousel exists — publishing is
  // offered after review, never before.
  useEffect(() => {
    if (!hasCarousel) return;
    let live = true;
    fetch("/api/reels/accounts", { cache: "no-store" })
      .then((r) => r.json())
      .then((body) => {
        if (!live) return;
        const rows = Array.isArray(body.platforms)
          ? body.platforms
              .filter((p: { connected?: boolean }) => p?.connected)
              .map((p: { platform: string; account?: { account_handle?: string } }) => ({
                platform: p.platform,
                account_handle: p.account?.account_handle ?? "",
                revoked_at: null,
              }))
          : [];
        setConnections(connectionStates(rows));
      })
      .catch(() => setConnections(connectionStates([])));
    return () => {
      live = false;
    };
  }, [hasCarousel]);

  // ── photo ──────────────────────────────────────────────────────────

  const onPhoto = useCallback((file: File | null) => {
    if (!file) return;
    if (!file.type.startsWith("image/")) return setError("Choose an image file.");
    if (file.size > 1_000_000) return setError("That photo is over 1MB.");
    const reader = new FileReader();
    reader.onload = () => {
      setPhoto(String(reader.result ?? ""));
      setError(null);
    };
    reader.onerror = () => setError("Could not read that file.");
    reader.readAsDataURL(file);
  }, []);

  // ── generation ─────────────────────────────────────────────────────

  async function generate() {
    if (!idea.trim()) return setError("Tell us what the carousel should be about.");
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch("/api/quotes/carousel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ idea, language, slides: slideCount, tone }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Could not write the carousel.");
      const next = normaliseSlides(data.slides, projectBackground);
      if (!next.length) throw new Error("Could not write the carousel.");
      setSlides(next);
      setWritten(data.language === "ar" ? "ar" : "en");
      setCaption("");
      setChosen([]);
      setConfirming(false);
    } catch (err) {
      setError(customerSafeError((err as Error).message));
    } finally {
      setBusy(false);
    }
  }

  /** Rewrite one slide, with its neighbours as context. Others are untouched. */
  async function regenerateSlide(index: number) {
    const slide = slides[index];
    if (!slide || regenerating) return;
    setRegenerating(slide.id);
    setError(null);
    try {
      const res = await fetch("/api/quotes/carousel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          action: "slide",
          idea,
          language: written,
          tone,
          role: slide.role,
          before: slides.slice(0, index).map((s) => s.text),
          after: slides.slice(index + 1).map((s) => s.text),
        }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Could not rewrite that slide.");
      setSlides((prev) =>
        prev.map((s, i) => (i === index ? { ...s, text: String(data.text ?? s.text) } : s)),
      );
    } catch (err) {
      setError(customerSafeError((err as Error).message));
    } finally {
      setRegenerating("");
    }
  }

  // ── slide editing ──────────────────────────────────────────────────

  function editSlide(index: number, text: string) {
    setSlides((prev) => prev.map((s, i) => (i === index ? { ...s, text } : s)));
  }

  function duplicateSlide(index: number) {
    setSlides((prev) => {
      const copy = makeSlide(prev[index].text, prev[index].role, prev[index].background);
      const next = [...prev];
      next.splice(index + 1, 0, copy);
      return withRoles(next);
    });
  }

  function deleteSlide(index: number) {
    setSlides((prev) => withRoles(prev.filter((_, i) => i !== index)));
  }

  function move(from: number, to: number) {
    setSlides((prev) => {
      if (to < 0 || to >= prev.length) return prev;
      const next = [...prev];
      const [item] = next.splice(from, 1);
      next.splice(to, 0, item);
      // Roles follow position: the slide now at the front is the hook.
      return withRoles(next);
    });
  }

  function setBackground(background: BackgroundId, index: number | "all") {
    setSlides((prev) =>
      prev.map((s, i) => (index === "all" || i === index ? { ...s, background } : s)),
    );
  }

  // ── saving ─────────────────────────────────────────────────────────

  async function saveProject() {
    if (!hasCarousel) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const payload = {
        topic: idea,
        language: written,
        fontId,
        ratio: ratioId,
        // The first slide's surface is the project default an old reader sees.
        backgroundId: slides[0]?.background ?? projectBackground,
        profile: {
          photo,
          displayName: name,
          username,
          preferredLanguage: written,
        },
        quotes: cardsFromSlides(slides),
      };
      const res = await fetch(
        initialProject
          ? `/api/quotes/projects/${initialProject.id}`
          : "/api/quotes/projects",
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

  const exportSlide = useCallback(
    async (slide: Slide, index: number) => {
      const canvas = document.createElement("canvas");
      canvas.width = ratio.w;
      canvas.height = ratio.h;
      const ctx = canvas.getContext("2d");
      if (!ctx) return;

      paintBackground(ctx, slide.background, canvas.width, canvas.height);
      await drawSlide(ctx, {
        text: slide.text,
        role: slide.role,
        width: canvas.width,
        height: canvas.height,
        rtl,
        font,
        ink: inkFor(backgroundById(slide.background)),
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
      // Zero-padded so the file order matches the carousel order.
      a.download = `slide-${String(index + 1).padStart(2, "0")}.png`;
      a.click();
      URL.revokeObjectURL(url);
    },
    [ratio, rtl, font, name, handle, photo],
  );

  /** One slide as PNG bytes, through the same renderer the export uses. */
  const renderSlide = useCallback(
    async (slide: Slide): Promise<Blob | null> => {
      const canvas = document.createElement("canvas");
      canvas.width = ratio.w;
      canvas.height = ratio.h;
      const ctx = canvas.getContext("2d");
      if (!ctx) return null;
      paintBackground(ctx, slide.background, canvas.width, canvas.height);
      await drawSlide(ctx, {
        text: slide.text,
        role: slide.role,
        width: canvas.width,
        height: canvas.height,
        rtl,
        font,
        ink: inkFor(backgroundById(slide.background)),
        name,
        handle,
        photo,
      });
      return new Promise((res) => canvas.toBlob(res, "image/png"));
    },
    [ratio, rtl, font, name, handle, photo],
  );

  async function exportAll() {
    setExporting({ done: 0, total: slides.length });
    for (let i = 0; i < slides.length; i++) {
      await exportSlide(slides[i], i);
      setExporting({ done: i + 1, total: slides.length });
      await new Promise((r) => setTimeout(r, 320));
    }
    setExporting({ done: 0, total: 0 });
  }

  // ── publishing ─────────────────────────────────────────────────────

  const selectable = useMemo(
    () => connections.filter((c) => c.connected && c.capability.implemented),
    [connections],
  );

  function togglePlatform(platform: string) {
    setChosen((prev) =>
      prev.includes(platform)
        ? prev.filter((p) => p !== platform)
        : [...prev, platform],
    );
    setConfirming(false);
  }

  // Each platform's caption: the edited one if there is one, else the
  // master trimmed to that platform's limit.
  const captionFor = Object.fromEntries(
    chosen.map((platform) => [
      platform,
      captions[platform] ??
        adaptCaption(
          caption,
          platform as never,
          CAPTION_LIMITS[platform as keyof typeof CAPTION_LIMITS] ?? 2200,
        ),
    ]),
  );

  // X carries four images. A longer carousel needs an answer before the
  // button is available -- never a silent truncation.
  const xNeedsDecision =
    chosen.includes("x") && slides.length > 4 && xOverflow === "undecided";

  const blocked = chosen
    .map((p) => connections.find((c) => c.platform === p))
    .filter(Boolean)
    .map((c) => canPublish(c as ConnectionState, true))
    .filter((r) => !r.allowed);

  // ── render ─────────────────────────────────────────────────────────

  return (
    <div className="studio carousel-studio">
      {error && (
        <p className="notice notice-error" role="alert">
          {error}
        </p>
      )}
      {notice && <p className="notice qs-ok">{notice}</p>}

      {/* ── 1. the idea ──────────────────────────────────────────── */}
      <section className="qs-panel cs-create">
        <h2 className="cs-h1">AI Carousel Studio</h2>
        <p className="cs-sub">
          Turn any idea into a ready-to-post social carousel.
        </p>

        <label className="field cs-idea">
          <span>What do you want to create a carousel about?</span>
          <textarea
            value={idea}
            onChange={(e) => setIdea(e.target.value)}
            rows={2}
            maxLength={300}
            dir="auto"
            placeholder={IDEA_EXAMPLES[0]}
          />
        </label>

        <div className="cs-examples">
          {IDEA_EXAMPLES.slice(1).map((example) => (
            <button
              key={example}
              type="button"
              className="cs-example"
              onClick={() => setIdea(example)}
            >
              {example}
            </button>
          ))}
        </div>

        <div className="cs-options">
          <div className="opt-group">
            <span className="opt-label">Language</span>
            <div className="seg seg-wrap">
              {CAROUSEL_LANGUAGES.map((l) => (
                <button
                  key={l.id}
                  type="button"
                  className={`seg-item${language === l.id ? " seg-on" : ""}`}
                  onClick={() => setLanguage(l.id)}
                >
                  {l.label}
                </button>
              ))}
            </div>
          </div>

          <div className="opt-group">
            <span className="opt-label">Slides</span>
            <div className="seg seg-wrap">
              {SLIDE_COUNTS.map((c) => (
                <button
                  key={c.id}
                  type="button"
                  className={`seg-item${slideCount === c.id ? " seg-on" : ""}`}
                  onClick={() => setSlideCount(c.id)}
                >
                  {c.label}
                </button>
              ))}
            </div>
          </div>

          <div className="opt-group">
            <span className="opt-label">Tone</span>
            <div className="seg seg-wrap">
              {TONES.map((t) => (
                <button
                  key={t.id}
                  type="button"
                  className={`seg-item${tone === t.id ? " seg-on" : ""}`}
                  onClick={() => setTone(t.id)}
                  title={t.hint}
                >
                  {t.label}
                </button>
              ))}
            </div>
          </div>
        </div>

        <button
          type="button"
          className="btn-generate cs-generate"
          onClick={generate}
          disabled={busy || !idea.trim()}
        >
          {busy ? "Writing…" : "Generate Carousel"}
        </button>
      </section>

      {/* ── 2. identity ──────────────────────────────────────────── */}
      {hasCarousel && (
        <section className="qs-panel">
          <h2 className="qs-h">Your details</h2>
          <div className="qs-brief">
            <div>
              <button
                type="button"
                className={`qs-drop${photo ? " has-photo" : ""}`}
                onClick={() => fileRef.current?.click()}
              >
                {photo ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img src={photo} alt="" className="qs-drop-img" />
                ) : (
                  <span className="qs-drop-empty">
                    <strong>Add a photo</strong>
                    <span>Square works best.</span>
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
                <input value={name} onChange={(e) => setName(e.target.value)} maxLength={60} />
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
            </div>
          </div>
        </section>
      )}

      {/* ── 3. design ────────────────────────────────────────────── */}
      {hasCarousel && (
        <section className="qs-panel">
          <h2 className="qs-h">Design</h2>

          <div className="opt-group">
            <span className="opt-label">Background — applies to every slide</span>
            <div className="qs-bg-grid">
              {BACKGROUNDS.map((b) => {
                const ink = inkFor(b);
                const on = slides.every((s) => s.background === b.id);
                return (
                  <button
                    key={b.id}
                    type="button"
                    className={`qs-bg${on ? " is-on" : ""}`}
                    onClick={() => setBackground(b.id, "all")}
                    title={b.hint}
                  >
                    <span className="qs-bg-chip" style={{ background: b.css }}>
                      <span className="qs-bg-aa" style={{ color: ink.ink }}>
                        {rtl ? "نص" : "Aa"}
                      </span>
                    </span>
                    <span className="qs-bg-label">{b.label}</span>
                  </button>
                );
              })}
            </div>
            <p className="opt-hint">
              Each slide can also be changed on its own, below.
            </p>
          </div>

          <div className="cs-two">
            <div className="opt-group">
              <span className="opt-label">Typeface</span>
              <div className="seg seg-wrap">
                {FONTS[written].map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    className={`seg-item${fontId === f.id ? " seg-on" : ""}`}
                    onClick={() => setFontId(f.id)}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            </div>
            <div className="opt-group">
              <span className="opt-label">Format</span>
              <div className="seg seg-wrap">
                {RATIOS.map((r) => (
                  <button
                    key={r.id}
                    type="button"
                    className={`seg-item${ratioId === r.id ? " seg-on" : ""}`}
                    onClick={() => setRatioId(r.id)}
                  >
                    {r.label}
                    <span className="seg-sub">{r.hint}</span>
                  </button>
                ))}
              </div>
            </div>
          </div>
        </section>
      )}

      {/* ── 4. review grid ───────────────────────────────────────── */}
      {hasCarousel && (
        <section className="qs-panel">
          <h2 className="qs-h">
            Your carousel
            <span className="qs-count">{slides.length} slides</span>
          </h2>

          {exporting.total > 0 && (
            <div className="qs-progress">
              <div
                className="qs-progress-bar"
                style={{ width: `${(exporting.done / Math.max(exporting.total, 1)) * 100}%` }}
              />
              <span>
                Exporting {exporting.done} of {exporting.total}…
              </span>
            </div>
          )}

          <div className="cs-grid">
            {slides.map((slide, i) => (
              <article
                key={slide.id}
                className={`cs-card${dragIndex === i ? " is-dragging" : ""}`}
                draggable
                onDragStart={() => setDragIndex(i)}
                onDragEnd={() => setDragIndex(null)}
                onDragOver={(e) => e.preventDefault()}
                onDrop={() => {
                  if (dragIndex !== null) move(dragIndex, i);
                  setDragIndex(null);
                }}
              >
                <div className="cs-card-head">
                  <span className="cs-slide-n">Slide {i + 1}</span>
                  <span className={`cs-role cs-role-${slide.role}`}>
                    {slide.role === "hook" ? "Hook" : slide.role === "cta" ? "Close" : "Body"}
                  </span>
                </div>

                <SlidePreview
                  slide={slide}
                  ratio={ratio}
                  rtl={rtl}
                  font={font}
                  name={name}
                  handle={handle}
                  photo={photo}
                />

                <textarea
                  className="cs-text"
                  value={slide.text}
                  dir={rtl ? "rtl" : "ltr"}
                  rows={3}
                  onChange={(e) => editSlide(i, e.target.value)}
                  style={{ fontFamily: font.stack }}
                />
                {!slideFits(slide.text, slide.role, written) && (
                  <p className="cs-warn">Long for a slide — it will be set smaller.</p>
                )}

                <div className="cs-card-tools">
                  <button type="button" onClick={() => move(i, i - 1)} disabled={i === 0} title="Move up">↑</button>
                  <button type="button" onClick={() => move(i, i + 1)} disabled={i === slides.length - 1} title="Move down">↓</button>
                  <button
                    type="button"
                    onClick={() => regenerateSlide(i)}
                    disabled={Boolean(regenerating)}
                    title="Rewrite this slide only"
                  >
                    {regenerating === slide.id ? "…" : "↻"}
                  </button>
                  <button type="button" onClick={() => duplicateSlide(i)} title="Duplicate">⧉</button>
                  <button
                    type="button"
                    onClick={() => deleteSlide(i)}
                    disabled={slides.length <= 1}
                    title="Delete"
                  >
                    ✕
                  </button>
                  <button type="button" onClick={() => exportSlide(slide, i)} title="Download">PNG</button>
                </div>

                <div className="cs-card-bg">
                  {BACKGROUNDS.map((b) => (
                    <button
                      key={b.id}
                      type="button"
                      className={`cs-bg-dot${slide.background === b.id ? " is-on" : ""}`}
                      style={{ background: b.css }}
                      onClick={() => setBackground(b.id, i)}
                      title={`${b.label} — this slide only`}
                      aria-label={`${b.label}, this slide only`}
                    />
                  ))}
                </div>
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
              Download All {slides.length}
            </button>
            <button type="button" className="btn-ghost" onClick={saveProject} disabled={busy}>
              {busy ? "Saving…" : "Save project"}
            </button>
          </div>
        </section>
      )}

      {/* ── 5. publish ───────────────────────────────────────────── */}
      {hasCarousel && (
        <section className="qs-panel">
          <h2 className="qs-h">Publish your carousel</h2>

          <div className="cs-platforms">
            {connections.map((c) => {
              const on = chosen.includes(c.platform);
              const note = coverageNote(c.platform, slides.length);
              return (
                <div key={c.platform} className={`cs-platform${on ? " is-on" : ""}`}>
                  <div className="cs-platform-head">
                    <b>{c.label}</b>
                    {c.connected ? (
                      <span className="cs-ok">Connected ✓</span>
                    ) : (
                      <a className="cs-connect" href="/dashboard/reels/accounts">
                        Connect
                      </a>
                    )}
                  </div>
                  {c.connected && c.handle && <p className="cs-handle">{c.handle}</p>}
                  <p className="cs-platform-note">{c.capability.format}</p>
                  {note && <p className="cs-warn">{note}</p>}
                  {!c.capability.implemented && (
                    <p className="cs-warn">Publishing here is not available yet.</p>
                  )}
                  <button
                    type="button"
                    className={`btn-ghost${on ? " is-on" : ""}`}
                    disabled={!c.connected || !c.capability.implemented}
                    onClick={() => togglePlatform(c.platform)}
                  >
                    {on ? "Selected" : "Select"}
                  </button>
                </div>
              );
            })}
          </div>

          <label className="field">
            <span>Post caption</span>
            <textarea
              value={caption}
              onChange={(e) => setCaption(e.target.value)}
              rows={3}
              dir={rtl ? "rtl" : "ltr"}
              placeholder="Write a caption, or leave it and add one later."
            />
          </label>
          {caption && chosen.length > 0 && (
            <ul className="cs-caption-previews">
              {chosen.map((platform) => {
                const limit = CAPTION_LIMITS[platform as keyof typeof CAPTION_LIMITS];
                const shaped = adaptCaption(caption, platform as never, limit ?? 2200);
                return (
                  <li key={platform}>
                    <b>{platform}</b>
                    <span>
                      {shaped.length} / {limit} characters
                      {shaped !== caption ? " — trimmed for this platform" : ""}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}

          {/* Nothing is ever sent without an explicit confirmation step. */}
          {xNeedsDecision && (
            <div className="cs-confirm">
              <h3>X carries 4 images</h3>
              <p>
                This carousel has {slides.length} slides. X posts at most four
                on one post.
              </p>
              <div className="qs-actions">
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => setXOverflow("trim")}
                >
                  Publish the first 4 to X
                </button>
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => togglePlatform("x")}
                >
                  Don&apos;t publish to X
                </button>
              </div>
            </div>
          )}

          {chosen.length > 0 && (
            <div className="cs-captions">
              <span className="opt-label">Caption per platform</span>
              {chosen.map((platform) => {
                const limit =
                  CAPTION_LIMITS[platform as keyof typeof CAPTION_LIMITS] ?? 2200;
                const value = captionFor[platform] ?? "";
                return (
                  <label key={platform} className="field">
                    <span>
                      {platform} · {value.length} / {limit}
                    </span>
                    <textarea
                      rows={2}
                      dir={rtl ? "rtl" : "ltr"}
                      value={value}
                      onChange={(e) =>
                        setCaptions((prev) => ({ ...prev, [platform]: e.target.value }))
                      }
                    />
                  </label>
                );
              })}
            </div>
          )}

          {publishResults.length > 0 && (
            <ul className="cs-results">
              {publishResults.map((r) => (
                <li key={r.platform} className={r.status === "published" ? "is-ok" : "is-bad"}>
                  <b>{r.platform}</b>
                  <span>
                    {r.status === "published" ? "✓ Published" : `✕ ${r.message}`}
                  </span>
                </li>
              ))}
            </ul>
          )}

          {publishState && <p className="opt-hint">{publishState}</p>}

          {!confirming ? (
            <button
              type="button"
              className="btn-generate"
              disabled={chosen.length === 0 || xNeedsDecision || busy}
              onClick={() => setConfirming(true)}
            >
              Review &amp; publish
            </button>
          ) : (
            <div className="cs-confirm">
              <h3>Ready to publish</h3>
              <p>{slides.length} slides to:</p>
              <ul>
                {chosen.map((p) => (
                  <li key={p}>
                    {connections.find((c) => c.platform === p)?.label ?? p} ✓
                  </li>
                ))}
              </ul>
              {blocked.length > 0 && <p className="cs-warn">{blocked[0].reason}</p>}
              <div className="qs-actions">
                <button
                  type="button"
                  className="btn-generate"
                  disabled={blocked.length > 0 || busy}
                  onClick={publishNow}
                >
                  {busy ? publishState || "Working…" : "Publish Now"}
                </button>
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => setConfirming(false)}
                  disabled={busy}
                >
                  Back
                </button>
              </div>
            </div>
          )}
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
  name,
  handle,
  photo,
}: {
  slide: Slide;
  ratio: { w: number; h: number };
  rtl: boolean;
  font: ReturnType<typeof fontById>;
  name: string;
  handle: string;
  photo: string;
}) {
  const background = backgroundById(slide.background);
  const ink = inkFor(background);
  // A hook is set larger, and a short line larger still: the type scales to
  // the text so a three-word slide is not lost in the middle of the frame.
  const size = slide.role === "hook" ? 19 : slide.text.length > 120 ? 13 : 15.5;

  return (
    <div
      className="cs-slide"
      dir={rtl ? "rtl" : "ltr"}
      style={{ aspectRatio: `${ratio.w} / ${ratio.h}`, background: background.css }}
    >
      <div className={`cs-slide-body${slide.role === "hook" ? " is-hook" : ""}`}>
        <p
          className="cs-slide-text"
          style={{
            fontFamily: font.stack,
            fontWeight: font.weight,
            lineHeight: font.lineHeight,
            letterSpacing: font.letterSpacing,
            color: ink.ink,
            fontSize: size,
          }}
        >
          {slide.text}
        </p>
        <div className="cs-slide-sig">
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
            {name && <span className="qs-sig-name" style={{ color: ink.ink }}>{name}</span>}
            {handle && <span className="qs-sig-handle" style={{ color: ink.secondary }}>{handle}</span>}
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
 * `direction` property, never from reversing the string — reversing breaks
 * the shaping and produces disconnected letters.
 */
function layoutLines(
  ctx: CanvasRenderingContext2D,
  text: string,
  maxWidth: number,
): string[] {
  const lines: string[] = [];
  // Honour deliberate line breaks, then wrap what is left.
  for (const paragraph of String(text ?? "").split("\n")) {
    const words = paragraph.trim().split(/\s+/).filter(Boolean);
    if (!words.length) continue;
    let line = "";
    for (const word of words) {
      const candidate = line ? `${line} ${word}` : word;
      if (ctx.measureText(candidate).width <= maxWidth || !line) line = candidate;
      else {
        lines.push(line);
        line = word;
      }
    }
    if (line) lines.push(line);
  }
  return lines;
}

async function drawSlide(
  ctx: CanvasRenderingContext2D,
  opts: {
    text: string;
    role: Slide["role"];
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
  const { text, role, width, height, rtl, font, ink, name, handle, photo } = opts;

  const pad = Math.round(width * 0.1);
  const maxWidth = width - pad * 2;
  ctx.direction = rtl ? "rtl" : "ltr";
  ctx.textAlign = rtl ? "right" : "left";
  const x = rtl ? width - pad : pad;

  // Fit by stepping down until the text fits its band. Starting large and
  // shrinking is what keeps a short hook big, which is the whole point of a
  // hook slide.
  const band = height * (role === "hook" ? 0.52 : 0.6);
  const min = Math.round(width * 0.035);
  let size = Math.round(width * (role === "hook" ? 0.105 : 0.072));
  let lines: string[] = [];

  for (; size > min; size -= 2) {
    ctx.font = `${font.weight} ${size}px ${font.stack}`;
    lines = layoutLines(ctx, text, maxWidth);
    if (lines.length * size * font.lineHeight <= band) break;
  }

  ctx.font = `${font.weight} ${size}px ${font.stack}`;
  const lineHeight = size * font.lineHeight;
  const sigBlock = Math.round(width * 0.16);
  const blockHeight = lines.length * lineHeight;
  // Hook slides sit centred; body slides sit above the signature so a long
  // and a short slide still share a baseline across the carousel.
  let y =
    role === "hook"
      ? (height - sigBlock - blockHeight) / 2
      : height - pad - sigBlock - blockHeight;

  ctx.fillStyle = ink.ink;
  for (const line of lines) {
    y += lineHeight;
    ctx.fillText(line, x, y);
  }

  const ruleY = height - pad - Math.round(width * 0.115);
  ctx.fillStyle = ink.rule;
  ctx.fillRect(rtl ? width - pad - maxWidth : pad, ruleY, maxWidth, 1);

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
      ctx.strokeStyle = ink.photoRing;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(cx, baseY, avatar / 2, 0, Math.PI * 2);
      ctx.stroke();
      textX = rtl
        ? x - avatar - Math.round(width * 0.03)
        : x + avatar + Math.round(width * 0.03);
    } catch {
      // An unreadable photo must not lose the whole export.
    }
  }

  if (name) {
    ctx.fillStyle = ink.ink;
    ctx.font = `700 ${Math.round(width * 0.032)}px ${font.stack}`;
    ctx.fillText(name, textX, baseY - Math.round(width * 0.004));
  }
  if (handle) {
    ctx.fillStyle = ink.secondary;
    ctx.font = `500 ${Math.round(width * 0.027)}px ${font.stack}`;
    ctx.fillText(handle, textX, baseY + Math.round(width * 0.035));
  }
}
