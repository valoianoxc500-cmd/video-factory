import { isWorkerAuthorized, workerUpdateJob, workerRegisterVideo } from "@/lib/worker-db";

/**
 * Worker progress and completion.
 *
 * On completion the worker also registers the finished video in the owner's
 * library. The owner is read from the job row inside the database function --
 * never accepted from this request -- so the worker cannot file a video under
 * an account of its choosing.
 */

export const dynamic = "force-dynamic";
export const maxDuration = 30;

interface UpdatePayload {
  id?: string;
  status?: string;
  progress?: number;
  stage?: string;
  message?: string;
  title?: string | null;
  error?: string | null;
  library?: {
    channelSlug: string;
    videoKey: string;
    videoPath: string;
    thumbnailPath?: string | null;
    description?: string;
    durationSeconds?: number | null;
    width?: number | null;
    height?: number | null;
    fps?: number | null;
    reviewStatus?: string;
    reviewLog?: unknown;
  };
}

export async function POST(request: Request) {
  if (!isWorkerAuthorized(request)) {
    return Response.json({ error: "Unauthorized" }, { status: 401 });
  }

  let body: UpdatePayload;
  try {
    body = (await request.json()) as UpdatePayload;
  } catch {
    return Response.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  const id = String(body.id ?? "").trim();
  if (!/^[0-9a-f-]{36}$/i.test(id)) {
    return Response.json({ error: "A valid job id is required" }, { status: 400 });
  }

  try {
    if (body.library) {
      const lib = body.library;
      await workerRegisterVideo({
        jobId: id,
        channelSlug: lib.channelSlug,
        videoKey: lib.videoKey,
        title: String(body.title ?? ""),
        description: lib.description,
        durationSeconds: lib.durationSeconds,
        width: lib.width,
        height: lib.height,
        fps: lib.fps,
        videoPath: lib.videoPath,
        thumbnailPath: lib.thumbnailPath,
        reviewStatus: lib.reviewStatus,
        reviewLog: lib.reviewLog,
      });
    }

    const job = await workerUpdateJob({
      id,
      status: body.status,
      stage: body.stage,
      progress:
        typeof body.progress === "number"
          ? Math.max(0, Math.min(100, body.progress))
          : undefined,
      message: body.message,
      title: body.title ?? undefined,
      error: body.error ?? null,
    });
    if (!job) {
      return Response.json({ error: "Job not found" }, { status: 404 });
    }
    return Response.json(job);
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    console.error("worker update failed:", message);
    return Response.json({ error: "Could not update the job." }, { status: 503 });
  }
}
