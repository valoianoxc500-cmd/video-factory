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

    def begin_resume(self) -> None:
        self.retry_count += 1
        self.started_at = now_iso()
        self.finished_at = None
        self.terminal_state = TerminalState.RUNNING
        self.recoverable = True
        self.recovery_reason = "resumed from valid checkpoint"

    def record_provider_failure(self, provider: str, operation: str, detail: str = "") -> None:
        self.provider_attempts.append(ProviderAttempt(
            provider=provider[:80], operation=operation[:80], outcome="failed",
            recoverable=True,
        ))
        self.recovery_reason = detail[:240]

    def finish(self, *, recoverable: bool, reason: str = "") -> None:
        self.finished_at = now_iso()
        self.terminal_state = TerminalState.FAILED
        self.recoverable = recoverable
        self.recovery_reason = reason[:240]

    def complete(self) -> None:
        self.finished_at = now_iso()
        self.terminal_state = TerminalState.COMPLETE
        self.recoverable = False
        self.recovery_reason = ""


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
