"""The publish queue: when a job runs, how often it retries, when it stops.

Why a queue at all
------------------
Two of the four publishable platforms have no native scheduling, uploads take
minutes, and every platform rate-limits. So a publish is a durable job rather
than a request handler: it has a due time, an attempt count, and a terminal
state that a person can read.

The isolation rule the worker must not break
--------------------------------------------
The worker runs across all users' jobs -- that is its purpose -- but a job
carries the id of the user who created it, and the token for that job is
resolved with *that* id. `run_due_jobs` never holds an ambient "current user".
If a job's owner and the account being used ever disagree, the job fails
rather than posting to whoever's account happened to be at hand.

Terminal versus retryable is decided once, in `record_result`, from the
adapter's own verdict: UNSUPPORTED never retries because it cannot succeed,
an auth or validation failure never retries because repeating it only burns
the user's rate limit, and transport failures retry on exponential backoff up
to `MAX_ATTEMPTS`.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from viral.publishing import (
    MAX_ATTEMPTS,
    PublishRequest,
    PublishResult,
    PublishStatus,
    backoff_seconds,
    plan_publication,
    validate_request,
)

logger = logging.getLogger("viral.queue")


class QueueError(Exception):
    """A queue operation that must not be allowed to proceed."""


@dataclass
class PublishJob:
    id: str
    user_id: str
    video_id: str
    platform: str
    caption: str = ""
    media_path: str = ""
    attribution: str = ""
    status: str = PublishStatus.QUEUED.value
    mode: str = "publish_now"
    attempts: int = 0
    scheduled_for: datetime | None = None
    next_attempt_at: datetime | None = None
    last_error: str = ""
    post_id: str = ""
    post_url: str = ""
    created_at: datetime | None = None
    completed_at: datetime | None = None

    @property
    def terminal(self) -> bool:
        return self.status in {
            PublishStatus.PUBLISHED.value,
            PublishStatus.UNSUPPORTED.value,
        } or (
            self.status == PublishStatus.FAILED.value
            and self.next_attempt_at is None
        )

    def due(self, now: datetime) -> bool:
        if self.status != PublishStatus.QUEUED.value:
            return False
        when = self.next_attempt_at or self.scheduled_for
        return when is None or when <= now

    def to_record(self) -> dict:
        """What the Queue screen shows. No token material passes through here."""
        return {
            "id": self.id,
            "video_id": self.video_id,
            "platform": self.platform,
            "status": self.status,
            "attempts": self.attempts,
            "scheduled_for": (
                self.scheduled_for.isoformat() if self.scheduled_for else None
            ),
            "next_attempt_at": (
                self.next_attempt_at.isoformat() if self.next_attempt_at else None
            ),
            "last_error": self.last_error,
            "post_url": self.post_url,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }


class JobQueue:
    """In-process queue with the same access rules as the database table.

    User-facing reads take a `user_id` and filter on it. `due` is the one
    method that spans users, because the worker legitimately serves everyone --
    and every job it returns still carries its own owner, which is what the
    token lookup uses.
    """

    def __init__(self) -> None:
        self._jobs: dict[str, PublishJob] = {}

    def enqueue(self, request: PublishRequest, *, now: datetime | None = None) -> list[PublishJob]:
        """Validate, plan, and store one job per selected platform."""
        reference = now or datetime.now(timezone.utc)
        problems = validate_request(request, now=reference)
        blocking = [p for p in problems if "Select at least one" in p
                    or "No processed video" in p
                    or "past" in p or "timezone" in p or "180 days" in p
                    or "Unknown platform" in p]
        if blocking:
            raise QueueError("; ".join(blocking))

        jobs: list[PublishJob] = []
        for planned in plan_publication(request, now=reference):
            job = PublishJob(
                id=secrets.token_hex(12),
                user_id=request.user_id,
                video_id=request.video_id,
                platform=planned["platform"],
                caption=planned.get("caption", ""),
                media_path=request.media_path,
                attribution=request.attribution,
                status=planned["status"],
                mode=planned.get("mode", "publish_now"),
                scheduled_for=request.scheduled_for,
                last_error=planned.get("reason", ""),
                created_at=reference,
            )
            if job.status == PublishStatus.UNSUPPORTED.value:
                job.completed_at = reference
            # Natively scheduled platforms are handed the time at upload, so
            # the job itself runs now.
            if job.mode == "native_schedule":
                job.next_attempt_at = reference
            elif job.mode == "queue_until_due":
                job.next_attempt_at = request.scheduled_for
            self._jobs[job.id] = job
            jobs.append(job)
        return jobs

    def get(self, user_id: str, job_id: str) -> PublishJob:
        job = self._jobs.get(job_id)
        if job is None:
            raise QueueError(f"No job {job_id!r}.")
        if job.user_id != user_id:
            logger.warning(
                f"cross-user job read refused: user={user_id} job={job_id}")
            raise QueueError("That job belongs to a different user.")
        return job

    def list_for_user(self, user_id: str, *, status: str = "") -> list[PublishJob]:
        return [
            job for job in self._jobs.values()
            if job.user_id == user_id and (not status or job.status == status)
        ]

    def due(self, now: datetime | None = None, *, limit: int = 25) -> list[PublishJob]:
        reference = now or datetime.now(timezone.utc)
        ready = [job for job in self._jobs.values() if job.due(reference)]
        ready.sort(key=lambda j: (
            j.next_attempt_at or j.scheduled_for or j.created_at
            or datetime.min.replace(tzinfo=timezone.utc)
        ))
        return ready[:limit]

    def cancel(self, user_id: str, job_id: str, *, now: datetime | None = None) -> PublishJob:
        job = self.get(user_id, job_id)
        if job.terminal:
            raise QueueError("That job has already finished.")
        job.status = PublishStatus.FAILED.value
        job.next_attempt_at = None
        job.last_error = "Cancelled."
        job.completed_at = now or datetime.now(timezone.utc)
        return job

    def save(self, job: PublishJob) -> PublishJob:
        self._jobs[job.id] = job
        return job


def record_result(
    job: PublishJob,
    result: PublishResult,
    *,
    now: datetime | None = None,
) -> PublishJob:
    """Fold an attempt's outcome into the job, deciding retry or stop."""
    reference = now or datetime.now(timezone.utc)
    job.attempts += 1

    if result.status is PublishStatus.PUBLISHED:
        job.status = PublishStatus.PUBLISHED.value
        job.post_id = result.post_id
        job.post_url = result.url
        job.next_attempt_at = None
        job.completed_at = reference
        # A published-with-caveat result (a TikTok draft) keeps its note.
        job.last_error = result.error
        return job

    if result.status is PublishStatus.UNSUPPORTED:
        job.status = PublishStatus.UNSUPPORTED.value
        job.last_error = result.error
        job.next_attempt_at = None
        job.completed_at = reference
        return job

    job.last_error = result.error
    if result.retryable and job.attempts < MAX_ATTEMPTS:
        job.status = PublishStatus.QUEUED.value
        job.next_attempt_at = reference + timedelta(
            seconds=backoff_seconds(job.attempts))
        return job

    job.status = PublishStatus.FAILED.value
    job.next_attempt_at = None
    job.completed_at = reference
    if not result.retryable:
        job.last_error = result.error
    elif job.attempts >= MAX_ATTEMPTS:
        job.last_error = (
            f"{result.error} (gave up after {job.attempts} attempts)"
        )
    return job


def run_due_jobs(
    queue: JobQueue,
    *,
    publisher,
    token_for,
    now: datetime | None = None,
    limit: int = 25,
) -> list[PublishJob]:
    """Run every due job, each against its own owner's account.

    `token_for(user_id, platform)` resolves the token; `publisher(job, token)`
    performs the upload and returns a `PublishResult`. Both are injected so
    the worker's decision-making is testable without a network or a database.
    """
    reference = now or datetime.now(timezone.utc)
    processed: list[PublishJob] = []

    for job in queue.due(reference, limit=limit):
        job.status = PublishStatus.PROCESSING.value
        queue.save(job)
        try:
            token = token_for(job.user_id, job.platform)
            result = publisher(job, token)
        except Exception as exc:
            # An account problem is the user's to fix; do not spin on it.
            result = PublishResult(
                platform=job.platform,
                status=PublishStatus.FAILED,
                error=str(exc)[:300],
                retryable=False,
            )
        queue.save(record_result(job, result, now=reference))
        processed.append(job)
    return processed


def queue_summary(queue: JobQueue, user_id: str) -> dict:
    """Counts for the dashboard, for this user only."""
    jobs = queue.list_for_user(user_id)
    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.status] = counts.get(job.status, 0) + 1
    scheduled = [
        j for j in jobs
        if j.status == PublishStatus.QUEUED.value and j.scheduled_for
    ]
    return {
        "total": len(jobs),
        "by_status": counts,
        "scheduled": len(scheduled),
        "next_due": min(
            (j.scheduled_for for j in scheduled if j.scheduled_for),
            default=None,
        ),
    }
