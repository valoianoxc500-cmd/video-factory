"""Rights gate: nothing is processed or published without a claim to use it.

Why this is the first module
----------------------------
Everything else in this product -- discovery, scoring, re-encoding, publishing
-- is only legitimate when the operator actually holds rights to the footage.
So the rights record is not a checkbox bolted on at the end; it is the thing
the pipeline asks for before it will touch a file.

What this deliberately does NOT do
----------------------------------
It does not evade anything. There is no fingerprint perturbation, no
duplicate-detection avoidance, no metadata laundering, no re-encode chosen to
defeat Content ID. Those would be the mechanics of infringement, and a tool
that ships them is a tool for infringing. Processing here is limited to
formatting and quality work that any honest editor would apply to their own
footage, and the platforms' own systems are left to do their job.

A discovered video is reference material for analysis. It is never an input to
publishing: `Source.DISCOVERED` cannot be attested, so the pipeline cannot be
walked from "I found this on TikTok" to "publish it to my account".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class Source(str, Enum):
    """Where a video came from, which decides what may be done with it."""

    OWN_RECORDING = "own_recording"      # the user filmed it
    LICENSED = "licensed"                # bought or licensed stock
    WRITTEN_PERMISSION = "permission"    # creator gave written permission
    PUBLIC_DOMAIN = "public_domain"
    CREATIVE_COMMONS = "creative_commons"
    # The single attestation the Add-a-video form collects: "I own this content
    # or have permission to reuse it." It merges OWN_RECORDING and
    # WRITTEN_PERMISSION without asking the user to say which, so it is
    # recorded as its own value rather than filed as either -- the audit trail
    # should say exactly what was claimed and no more.
    OWNED_OR_PERMITTED = "owned_or_permitted"
    DISCOVERED = "discovered"            # found via search: analysis only


#: Sources that may be processed and published.
PUBLISHABLE_SOURCES = frozenset({
    Source.OWN_RECORDING,
    Source.LICENSED,
    Source.WRITTEN_PERMISSION,
    Source.PUBLIC_DOMAIN,
    Source.CREATIVE_COMMONS,
    Source.OWNED_OR_PERMITTED,
})

#: Licences that require crediting the rights holder wherever it is published.
ATTRIBUTION_REQUIRED = frozenset({Source.CREATIVE_COMMONS, Source.WRITTEN_PERMISSION})


class RightsError(RuntimeError):
    """Raised when an action is attempted without a usable rights record."""


@dataclass(frozen=True)
class RightsAttestation:
    """The user's declaration that they may use a specific video.

    Frozen on purpose: an attestation is a record of something the user
    asserted at a moment in time, not a mutable field. Changing the claim
    means making a new one.
    """

    source: Source
    attested_by: str                     # user id, from the verified session
    attested_at: datetime
    #: Who to credit. Required when the licence demands attribution.
    rights_holder: str = ""
    #: Free text: licence number, the permission email, the CC deed URL.
    evidence: str = ""
    notes: str = ""

    def __post_init__(self) -> None:
        if not str(self.attested_by).strip():
            raise RightsError("an attestation must record who made it")
        if self.attested_at.tzinfo is None:
            raise RightsError("attested_at must be timezone-aware")

    @property
    def is_publishable(self) -> bool:
        return self.source in PUBLISHABLE_SOURCES

    @property
    def needs_attribution(self) -> bool:
        return self.source in ATTRIBUTION_REQUIRED

    def blocking_reason(self) -> str:
        """Why this attestation cannot support publishing, or "" if it can."""
        if self.source == Source.DISCOVERED:
            return (
                "Discovered videos are reference material for analysis only. "
                "To publish, import a copy you recorded, licensed, or have "
                "written permission to reuse, and attest to that."
            )
        if not self.is_publishable:
            return f"{self.source.value} is not a publishable rights basis"
        if self.needs_attribution and not self.rights_holder.strip():
            return (
                f"{self.source.value} requires crediting the rights holder; "
                f"record who to credit before publishing"
            )
        if self.source in (Source.LICENSED, Source.WRITTEN_PERMISSION) and not (
            self.evidence.strip()
        ):
            return (
                f"{self.source.value} requires evidence -- the licence "
                f"reference or the permission itself"
            )
        return ""

    def to_record(self) -> dict:
        return {
            "source": self.source.value,
            "attested_by": self.attested_by,
            "attested_at": self.attested_at.isoformat(),
            "rights_holder": self.rights_holder,
            "evidence": self.evidence,
            "notes": self.notes,
            "needs_attribution": self.needs_attribution,
        }


def attest(
    *,
    source: Source | str,
    user_id: str,
    rights_holder: str = "",
    evidence: str = "",
    notes: str = "",
    now: datetime | None = None,
) -> RightsAttestation:
    """Record a rights claim. Raises rather than returning an unusable one."""
    if isinstance(source, str):
        try:
            source = Source(source)
        except ValueError as exc:
            raise RightsError(f"unknown rights source {source!r}") from exc
    return RightsAttestation(
        source=source,
        attested_by=str(user_id),
        attested_at=now or datetime.now(timezone.utc),
        rights_holder=rights_holder.strip(),
        evidence=evidence.strip(),
        notes=notes.strip(),
    )


def require_publishable(attestation: RightsAttestation | None) -> RightsAttestation:
    """Gate before processing or publishing. Raises with a usable message."""
    if attestation is None:
        raise RightsError(
            "No rights attestation for this video. Record how you are entitled "
            "to use it before importing or publishing."
        )
    reason = attestation.blocking_reason()
    if reason:
        raise RightsError(reason)
    return attestation


# ---------------------------------------------------------------------------
# Refusals that must survive future edits
# ---------------------------------------------------------------------------

#: Techniques whose only purpose is to defeat a platform's own enforcement.
#: Named so that a request for one is refused explicitly rather than quietly
#: implemented under a friendlier label.
PROHIBITED_TECHNIQUES = (
    "content id evasion",
    "fingerprint perturbation",
    "duplicate detection evasion",
    "watermark removal",
    "metadata laundering",
    "audio pitch shift to avoid matching",
    "mirror flip to avoid matching",
    "frame injection to defeat hashing",
    "moderation bypass",
)


def is_prohibited_technique(name: str) -> bool:
    """Whether a requested processing step is an evasion technique."""
    text = re.sub(r"[^a-z ]+", " ", str(name or "").lower())
    text = " ".join(text.split())
    if not text:
        return False
    for banned in PROHIBITED_TECHNIQUES:
        if banned in text:
            return True
    # Catch the intent even when phrased differently.
    evasive = ("evade", "evasion", "bypass", "defeat", "avoid detection",
               "circumvent", "spoof", "launder")
    target = ("content id", "copyright", "fingerprint", "duplicate",
              "moderation", "detection", "hash", "matching")
    return any(e in text for e in evasive) and any(t in text for t in target)
