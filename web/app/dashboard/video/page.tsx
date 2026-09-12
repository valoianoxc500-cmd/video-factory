import { VideoMaker } from "@/components/aivideo/VideoMaker";
import "../../aivideo.css";

/**
 * AI Video Maker — the primary product.
 *
 * Topic in, finished short-form video out. Independent of the channel
 * pipeline and of Quote Studio: its own table, its own queue and its own
 * worker, so a stuck generation here cannot hold up anything else.
 *
 * A client component below because the whole page is one live form: the
 * caption preview, the voice samples and the generation progress all react to
 * state that only exists in the browser.
 */

export const dynamic = "force-dynamic";

export const metadata = {
  title: "AI Video Maker",
};

export default function AiVideoMakerPage() {
  return (
    <>
      <div className="page-head" data-art="aivideo">
        <h1>AI Video Maker</h1>
        <p>
          Describe an idea and get a finished short-form video: script,
          footage, voice-over and captions, in English or Arabic. Everything
          below is optional — the defaults are already good.
        </p>
      </div>
      <VideoMaker />
    </>
  );
}
