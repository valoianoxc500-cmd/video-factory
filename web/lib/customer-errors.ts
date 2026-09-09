/** Customer-safe presentation for operational failures.
 * Raw diagnostics remain in worker logs/checkpoints and never cross a
 * customer route boundary through this mapper. */

const INTERNAL = /traceback|stack|exception|error:|provider|gemini|fal|pexels|pixabay|unsplash|http|api|json|\\\\|\/[\w.-]+\/|\b(?:c:|enospc|errno|ffmpeg|ffprobe)\b/i;

export function customerSafeError(raw: unknown): string {
  const text = String(raw ?? "").trim();
  if (!text) return "We could not finish this generation. Your work has been saved and can be retried.";
  if (/rate limit|too many requests/i.test(text)) return "A service is busy right now. Your generation is saved and will be retried safely.";
  if (/timeout|timed out|network|connect/i.test(text)) return "A service took too long to respond. Your generation is saved and can resume safely.";
  if (/missing|not found|corrupt|invalid.*(?:file|image|video|output)/i.test(text)) return "One required asset could not be prepared. Your generation is saved and can be retried.";
  if (INTERNAL.test(text)) return "We could not finish this generation. Your work has been saved and can be retried.";
  return text.length <= 180 ? text : "We could not finish this generation. Your work has been saved and can be retried.";
}

export function customerJobMessage(status: string, stage: string, raw: unknown): string {
  if (status === "done") return "Complete";
  if (status === "error") return customerSafeError(raw);
  const states: Record<string, string> = {
    queued: "Preparing", planning: "Researching", script: "Creating",
    character: "Creating", image_source: "Creating", animation: "Creating",
    audio_source: "Creating", process: "Creating", render_sections: "Rendering",
    assemble: "Rendering", thumbnail: "Almost ready", final_review: "Almost ready",
  };
  return states[stage] ?? "Preparing";
}
