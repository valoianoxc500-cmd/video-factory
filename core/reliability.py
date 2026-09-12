"""Small, dependency-free reliability contracts shared by pipeline stages.

This module deliberately does not replace providers or the pipeline.  It
records the recovery decision around an existing run, gives stages a common
fallback vocabulary, and provides atomic artifact promotion helpers.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TerminalState(str, Enum):
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"


class FailureKind(str, Enum):
    """Why a run stopped, in the only two categories that change what happens.

    RECOVERABLE means trying again could plausibly succeed with no new input
    from the customer: a refused quota, a 5xx, a timeout, a sourcing provider
    having a bad ten minutes. The checkpoint is kept and the job is requeued.

    FACTUAL means the run is blocked on evidence, and retrying the exact same
    request would burn the same money to reach the same wall. Only the customer
    can unblock it, so the run is left terminal and told them what to add.

    Kept deliberately coarse. Anything not positively identified as factual is
    treated as recoverable and bounded by the retry ceiling, because guessing
    "terminal" wrongly strands a paying run that a retry would have finished.
    """

    RECOVERABLE = "recoverable"
    FACTUAL = "factual"


#: Result vocabulary for a finished run. `completed` and the two failure states
#: are what a future credit policy would read; nothing here decides or records
#: money, it only makes the three outcomes distinguishable.
RESULT_COMPLETED = "completed"
RESULT_RECOVERABLE_FAILED = "recoverable_failed"
RESULT_TERMINAL_FAILED = "terminal_failed"


def result_status(*, completed: bool, recoverable: bool) -> str:
    if completed:
        return RESULT_COMPLETED
    return RESULT_RECOVERABLE_FAILED if recoverable else RESULT_TERMINAL_FAILED


#: Backoff between automatic resumes, in seconds, indexed by how many have
#: already happened. Bounded on both ends. The first step outlasts the model
#: quota cooldown in clients.py, so an immediate resume cannot walk straight
#: back into the refusal that stopped it; the last is capped at five minutes
#: because the worker is a singleton and waiting here also holds up whoever is
#: behind this job in the queue.
_RETRY_BACKOFF_SECONDS = (180.0, 300.0)


def retry_delay_seconds(retry_count: int) -> float:
    """How long to wait before resuming, after `retry_count` prior attempts."""
    if retry_count < 0:
        retry_count = 0
    index = min(retry_count, len(_RETRY_BACKOFF_SECONDS) - 1)
    return _RETRY_BACKOFF_SECONDS[index]


class ProviderAttempt(BaseModel):
    provider: str = ""
    operation: str = ""
    outcome: str = ""
    recoverable: bool = True
    at: str = Field(default_factory=now_iso)


class RunAttempt(BaseModel):
    """Durable state embedded in the existing checkpoint.json format."""

    attempt_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    retry_count: int = 0
    provider_attempts: list[ProviderAttempt] = Field(default_factory=list)
    started_at: str = Field(default_factory=now_iso)
    finished_at: str | None = None
    terminal_state: TerminalState = TerminalState.RUNNING
    recoverable: bool = True
    recovery_reason: str = ""
    failure_kind: FailureKind | None = None

    def begin_resume(self) -> None:
        self.retry_count += 1
        self.started_at = now_iso()
        self.finished_at = None
        self.terminal_state = TerminalState.RUNNING
        self.recoverable = True
        self.recovery_reason = "resumed from valid checkpoint"
        self.failure_kind = None

    def record_provider_failure(self, provider: str, operation: str, detail: str = "") -> None:
        self.provider_attempts.append(ProviderAttempt(
            provider=provider[:80], operation=operation[:80], outcome="failed",
            recoverable=True,
        ))
        self.recovery_reason = detail[:240]

    def finish(
        self,
        *,
        recoverable: bool,
        reason: str = "",
        kind: FailureKind | None = None,
    ) -> None:
        self.finished_at = now_iso()
        self.terminal_state = TerminalState.FAILED
        self.recoverable = recoverable
        self.recovery_reason = reason[:240]
        self.failure_kind = kind or (
            FailureKind.RECOVERABLE if recoverable else FailureKind.FACTUAL
        )

    def complete(self) -> None:
        self.finished_at = now_iso()
        self.terminal_state = TerminalState.COMPLETE
        self.recoverable = False
        self.recovery_reason = ""
        self.failure_kind = None

    @property
    def result_status(self) -> str:
        return result_status(
            completed=self.terminal_state is TerminalState.COMPLETE,
            recoverable=self.recoverable,
        )


# ── What the customer is told ────────────────────────────────────────
#
# One place, so no caller has to decide how much of a provider incident to
# leak. Nothing in here names a model, a vendor, an HTTP status, a stage or a
# file path -- a customer gets a state and, when it is their call to make, the
# one thing they can do about it.

#: Shown while the run is still working. Keyed by pipeline stage.
PROGRESS_MESSAGES: dict[str, str] = {
    "planning": "Researching verified sources…",
    "script": "Writing your video…",
    "image_source": "Finding match visuals…",
    "audio_source": "Recording the narration…",
    "animation": "Building remaining visuals…",
    "process": "Building remaining visuals…",
    "render_sections": "Rendering your video…",
    "assemble": "Rendering your video…",
    "thumbnail": "Finalizing…",
    "final_review": "Finalizing…",
}

#: Shown when a provider is degraded but the run is still going. Deliberately
#: says nothing about which provider, or that anything is wrong at all beyond
#: "slower" -- from the customer's side that is the whole truth.
DEGRADED_MESSAGE = "Still working — we're using another source."

#: Shown when the run has stopped and will resume on its own.
RECOVERABLE_MESSAGE = (
    "Generation is temporarily delayed. Your progress is saved and will "
    "retry automatically."
)

#: Shown when the run has stopped and only the customer can unblock it.
FACTUAL_MESSAGE = (
    "We couldn't verify this match. Add the match date or competition."
)

#: The last resort: retries are spent, or the checkpoint could not be read.
#: Still not a blame message -- the topic is not at fault for a system that
#: ran out of attempts.
EXHAUSTED_MESSAGE = (
    "This generation could not be completed. Your topic is fine — please try "
    "again."
)


def progress_message(stage: str) -> str:
    return PROGRESS_MESSAGES.get(stage, "Working on your video…")


def customer_failure_message(
    kind: FailureKind | str | None,
    *,
    will_retry: bool,
) -> str:
    """The customer-facing sentence for a stopped run.

    `will_retry` is what actually happens next, not what the failure was. A
    recoverable failure that has run out of attempts must not keep promising an
    automatic retry that is never coming -- and it must still not blame the
    topic, because the topic was never the problem.
    """
    if kind in (FailureKind.FACTUAL, FailureKind.FACTUAL.value):
        return FACTUAL_MESSAGE
    return RECOVERABLE_MESSAGE if will_retry else EXHAUSTED_MESSAGE


class StageFallbackContract(BaseModel):
    primary: str
    retry: bool = True
    secondary: str = ""
    local_fallback: str = ""
    safe_generated_fallback: str = ""
    continue_on_exhaustion: bool = False


# A contract is documentation the code can persist and test; it does not force
# a fallback which a channel's truth or quality policy forbids.
FALLBACK_CONTRACTS: dict[str, StageFallbackContract] = {
    "planning": StageFallbackContract(primary="grounded research", secondary="cached/fixture research"),
    "image_source": StageFallbackContract(primary="configured licensed provider", secondary="configured free provider", safe_generated_fallback="channel-approved generated visual", continue_on_exhaustion=True),
    "audio_source": StageFallbackContract(primary="configured narration provider", local_fallback="deterministic caption timing", continue_on_exhaustion=False),
    "process": StageFallbackContract(primary="validated source artifact", local_fallback="existing valid artifact", continue_on_exhaustion=False),
    "render_sections": StageFallbackContract(primary="Remotion render", local_fallback="existing valid section render", continue_on_exhaustion=False),
    "assemble": StageFallbackContract(primary="FFmpeg assembly", local_fallback="validated completed output", continue_on_exhaustion=False),
    "thumbnail": StageFallbackContract(primary="configured thumbnail provider", local_fallback="existing valid thumbnail", continue_on_exhaustion=True),
    "final_review": StageFallbackContract(primary="quality review", retry=False, continue_on_exhaustion=False),
}


def fallback_contract(stage: str) -> StageFallbackContract:
    return FALLBACK_CONTRACTS.get(stage, StageFallbackContract(primary="existing stage implementation"))


_RUN_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,80}$")


def deterministic_workspace(root: Path, channel: str, run_id: str) -> Path:
    """Return the stable workspace for a queued job without changing legacy names."""
    if not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError("run id must be a UUID-like identifier")
    if not re.fullmatch(r"[a-z0-9_]{1,64}", channel):
        raise ValueError("channel must be a safe slug")
    return root / f"{channel}_job_{run_id}"


def promote_artifact(candidate: Path, destination: Path, valid: Callable[[Path], bool]) -> bool:
    """Atomically replace destination only after candidate validation.

    A failed generation never deletes the last good frame.  ``os.replace`` is
    atomic on the worker filesystem and candidate cleanup is best effort.
    """
    try:
        if not candidate.exists() or not valid(candidate):
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(candidate, destination)
        return True
    finally:
        if candidate.exists():
            candidate.unlink(missing_ok=True)
