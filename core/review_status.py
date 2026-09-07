"""Turn review-gate verdicts into the status a user sees on their video.

Why this exists
---------------
The gates already grade their findings. `image_review` marks each image
"ok" / "warning" / "error"; `final_review` separates `critical_issues` from
`minor_issues`. All of that detail was being thrown away: any gate with
flagged=true produced "flagged: <gate>" on the video, so a stock watermark
noticed in passing read exactly like a fabricated document.

It also let a demonstrably wrong judgement condemn a good video. On the
Flight 19 run the reviewer listed as critical:

    "the red highlight ... does not match the specific hex code #e23b3b
     ... it appears as a standard saturated red (#FF0000)"

Sampling the rendered frames put the highlight at #E23A3B-#E23B3C in 27 of 27
samples -- 1 to 3 units from the configured colour and about 88 from #FF0000.
A vision model cannot read a hex value off a compressed frame, so it guessed,
and the guess was wrong.

What this module does and does not do
-------------------------------------
It does NOT run, relax, re-score or bypass any gate. Every gate still runs,
still returns its own verdict, and the complete verdict is still stored in
`review_log` for inspection. This only decides which findings are strong
enough to put "Flagged" in front of a user.

A finding is discounted only when it is BOTH:
  * a claim about an exact machine-checkable value (a hex colour), and
  * of a kind a vision model has been measured to get wrong.

Everything else -- wrong subject, watermark, fabricated imagery, unsafe
content, irrelevant filler -- stays critical and stays flagged.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("video_factory")

STATUS_APPROVED = "approved"
STATUS_NOTES = "approved with notes"

# A finding that turns on an exact colour value. The reviewer sees a
# compressed frame; it cannot measure a hex code, and when it tried it was
# wrong by ~88 units in every sample. Colour is verified from config and by
# measuring the render, not by asking a model to eyeball it.
_COLOUR_CLAIM = re.compile(
    r"(#[0-9a-f]{3,8}\b"                       # an explicit hex code
    r"|\bhex\s*(code|colou?r|value)\b"
    r"|\bcolou?r\s*(code|value|hex)\b)",
    re.I,
)
# ... but only when the finding is ABOUT the colour matching, rather than a
# real problem that merely mentions one.
_COLOUR_SUBJECT = re.compile(
    r"\b(highlight|caption|subtitle|text|brand(ing)?\s*colou?r|palette)\b", re.I
)


def is_unreliable_finding(text: str) -> bool:
    """Whether a finding is a colour-match claim a vision model cannot judge.

    Deliberately narrow. A finding that merely mentions a colour while
    describing something real ("a red stock watermark covers the frame") is
    not discounted.
    """
    finding = str(text or "")
    if not _COLOUR_CLAIM.search(finding):
        return False
    if not _COLOUR_SUBJECT.search(finding):
        return False
    # A colour claim that also reports a substantive defect is kept.
    substantive = re.compile(
        r"\b(watermark|wrong|missing|unrelated|irrelevant|fabricat|"
        r"gore|nsfw|unsafe|illegible|gibberish|corrupt|blank|black screen)\b",
        re.I,
    )
    return not substantive.search(finding)


def _entry_findings(gate: str, entry: dict) -> tuple[list[str], list[str]]:
    """(critical, minor) findings for one gate, using its own grading."""
    critical: list[str] = []
    minor: list[str] = []
    if not isinstance(entry, dict):
        return critical, minor

    for attempt in entry.get("review_history") or []:
        if not isinstance(attempt, dict):
            continue

        # final_review grades at the video level.
        critical.extend(str(x) for x in (attempt.get("critical_issues") or []))
        minor.extend(str(x) for x in (attempt.get("minor_issues") or []))

        # image_review grades per image.
        for result in attempt.get("image_results") or []:
            if not isinstance(result, dict):
                continue
            issues = [str(i) for i in (result.get("issues") or [])]
            if not issues:
                continue
            where = f"section {result.get('section_id')} image {result.get('sub_image_index')}"
            severity = str(result.get("severity") or "").lower()
            target = critical if severity == "error" else minor
            target.extend(f"{where}: {issue}" for issue in issues)

    return critical, minor


def classify_gate(gate: str, entry: dict) -> dict:
    """Grade one gate: did it find anything that should flag the video?"""
    critical, minor = _entry_findings(gate, entry)
    discounted = [f for f in critical if is_unreliable_finding(f)]
    kept = [f for f in critical if f not in discounted]

    # A gate that reported no structured findings at all, yet did not
    # approve, is trusted: the shape is unknown, so its own verdict stands.
    unstructured = not critical and not minor
    flagged = bool(kept) or (
        unstructured and isinstance(entry, dict) and entry.get("flagged")
    )

    return {
        "gate": gate,
        "flagged": flagged,
        "critical": kept,
        "discounted": discounted,
        "minor": minor,
    }


def classify_review_log(review_log: dict) -> tuple[str, dict]:
    """Overall status string plus the per-gate detail behind it.

    Returns (status, detail). `status` is what goes on the video row:
    "approved", "approved with notes", or "flagged: <gates>".
    """
    gates: list[dict] = []
    for gate, entry in sorted((review_log or {}).items()):
        if isinstance(entry, dict):
            gates.append(classify_gate(gate, entry))

    flagged = [g["gate"] for g in gates if g["flagged"]]
    discounted = [f for g in gates for f in g["discounted"]]
    minor = [f for g in gates for f in g["minor"]]

    for finding in discounted:
        logger.info(
            f"review finding discounted as an unreliable colour judgement: "
            f"{finding[:160]}"
        )

    if flagged:
        status = "flagged: " + ", ".join(sorted(flagged))
    elif minor or discounted:
        status = STATUS_NOTES
    else:
        status = STATUS_APPROVED

    return status, {
        "gates": gates,
        "discounted_count": len(discounted),
        "minor_count": len(minor),
    }
