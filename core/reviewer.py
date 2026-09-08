"""Universal review gate engine — shared by all 4 review gates.

Each gate follows the same pattern:
1. Send content to AI reviewer with criteria-specific prompt
2. If approved, return content
3. If rejected, feed feedback back to regenerator for targeted fix
4. After max_attempts, fail the run with the review feedback
"""

import json
import logging
from pathlib import Path
from typing import Any, Callable, Awaitable

import clients

#: How many times to re-ask when the reviewer's response will not parse.
#: A truncated body is a transport problem, so it does not spend one of the
#: gate's content attempts -- but it is bounded, so a reviewer that is
#: persistently broken still fails the gate rather than looping forever.
_MALFORMED_REVIEW_RETRIES = 3

logger = logging.getLogger("video_factory")


class ReviewGateError(RuntimeError):
    """Raised when a review gate exhausts retries without approval."""

    def __init__(self, gate_name: str, result: dict[str, Any]):
        self.gate_name = gate_name
        self.result = result
        attempts = result.get("attempts", 0)
        feedback = result.get("feedback") or "no feedback"
        super().__init__(
            f"{gate_name} failed review after {attempts} attempt(s): {feedback}"
        )


def _image_result_is_approved(result: dict[str, Any]) -> bool:
    if "approved" in result:
        return bool(result["approved"])

    severity = result.get("severity")
    if isinstance(severity, str):
        return severity.lower() != "error"

    return False


def _normalize_raw_list_review(review: list[Any]) -> dict[str, Any]:
    image_results = []
    for item in review:
        if not isinstance(item, dict):
            raise ValueError(f"Image review returned non-object entry: {item!r}")
        normalized = dict(item)
        normalized["approved"] = _image_result_is_approved(normalized)
        image_results.append(normalized)

    rejected_count = sum(1 for item in image_results if not item["approved"])
    return {
        "approved": rejected_count == 0,
        "image_results": image_results,
        "feedback": f"Raw list response: {rejected_count} rejected image(s)",
    }


def require_slot_identity(review: Any, gate_name: str) -> None:
    """A rejection must say which image it is about.

    Raises ValueError -- the same signal a truncated body raises -- so the
    caller's malformed-response path re-asks rather than acting on it.

    Positional inference is deliberately not offered as a fallback. The
    reviewer numbers the images it was shown, and that numbering shifts
    whenever a slot has no asset, so position identifies the wrong beat. An
    unidentified rejection previously caused the gate to abandon regeneration
    entirely and burn both attempts having re-sourced nothing.
    """
    if gate_name != "image_review" or not isinstance(review, dict):
        return

    missing = 0
    for item in review.get("image_results") or []:
        if not isinstance(item, dict) or item.get("approved", True):
            continue
        try:
            int(item["section_id"])
            int(item["sub_image_index"])
        except (KeyError, TypeError, ValueError):
            missing += 1

    if missing:
        raise ValueError(
            f"{missing} rejected image(s) carried no usable "
            f"section_id/sub_image_index, so the rejection cannot be tied to "
            f"a beat"
        )


async def review_gate(
    content: Any,
    review_prompt_fn: Callable[[Any], str],
    system_instruction: str,
    regenerate_fn: Callable[[Any, str], Awaitable[Any]] | None = None,
    max_attempts: int = 3,
    gate_name: str = "review",
    image_paths: list[Path] | None = None,
) -> dict:
    """Universal review gate.

    Args:
        content: The content to review (script dict, image paths, etc.)
        review_prompt_fn: Function that takes content and returns the review prompt
        system_instruction: System prompt for the AI reviewer
        regenerate_fn: Async function(content, feedback) -> new content. If None, no regen.
        max_attempts: Max review cycles before flagging
        gate_name: Name for logging (e.g. "script_review", "image_review")
        image_paths: Optional list of image paths for vision-based review

    Returns:
        {
            "approved": bool,
            "content": <final content>,
            "attempts": int,
            "feedback": str | None,
            "scores": dict | None,
            "flagged_for_review": bool
        }

    Raises:
        ReviewGateError: If max_attempts is exhausted without approval.
    """
    best_content = content
    last_feedback = None
    last_scores = None
    review_history = []

    for attempt in range(1, max_attempts + 1):
        if attempt > 1 and regenerate_fn and last_feedback:
            logger.info(f"[{gate_name}] Regenerating (attempt {attempt})...")
            content = await regenerate_fn(content, last_feedback)

        prompt = review_prompt_fn(content)

        async def _ask_reviewer():
            if image_paths:
                answer = await clients.review_with_vision(
                    prompt,
                    image_paths,
                    system_instruction=system_instruction,
                    operation_label=gate_name,
                )
            else:
                answer = await clients.generate_json(
                    prompt,
                    system_instruction=system_instruction,
                    temperature=0.3,
                    operation_label=gate_name,
                )
            # Normalised here so the identity check sees the same shape the
            # gate will act on, whichever form the model replied in.
            if isinstance(answer, list):
                answer = _normalize_raw_list_review(answer)
            # A rejection that names no image is unusable, and inferring the
            # image from its position is how the wrong beat gets regenerated.
            # Treated as malformed so the retry below re-asks for it.
            require_slot_identity(answer, gate_name)
            return answer

        # A malformed response is not a verdict.
        #
        # Reviewing seventeen images produces a long JSON body, and when it is
        # truncated the decoder raises "Unterminated string". That was being
        # turned into `approved: False` with no per-image detail -- which both
        # spent a content attempt and left the regenerate step nothing to
        # target, so a run could fail the gate without the reviewer ever
        # having judged the images.
        #
        # Asking again is not weakening anything: a parse failure never
        # approves, and a genuine rejection still falls straight through.
        review = None
        for parse_attempt in range(1, _MALFORMED_REVIEW_RETRIES + 1):
            try:
                review = await _ask_reviewer()
                break
            except (json.JSONDecodeError, ValueError) as e:
                if parse_attempt == _MALFORMED_REVIEW_RETRIES:
                    logger.error(
                        f"[{gate_name}] Review response was unparseable after "
                        f"{parse_attempt} attempt(s): {e}"
                    )
                    review = {
                        "approved": False,
                        "feedback": f"Review error: {e}",
                    }
                    break
                logger.warning(
                    f"[{gate_name}] Review response was unparseable "
                    f"({str(e)[:80]}); asking again "
                    f"({parse_attempt}/{_MALFORMED_REVIEW_RETRIES})"
                )
            except Exception as e:
                logger.error(f"[{gate_name}] Review call failed: {e}")
                review = {"approved": False, "feedback": f"Review error: {e}"}
                break

        # Gemini sometimes returns the image_results array without the
        # top-level object; normalize it so regeneration sees rejected slots.
        if isinstance(review, list):
            review = _normalize_raw_list_review(review)

        approved = review.get("approved", False)
        feedback = review.get("feedback", "")
        scores = review.get("scores", None)

        review_history.append({"attempt": attempt, **review})

        if scores:
            last_scores = scores
        # Pass the full review dict so regenerate_fn can access
        # structured data (e.g. per-image results in image_review).
        last_feedback = review

        if approved:
            logger.info(f"[{gate_name}] APPROVED on attempt {attempt}")
            if scores:
                logger.info(f"[{gate_name}] Scores: {scores}")
            return {
                "approved": True,
                "content": content,
                "attempts": attempt,
                "feedback": None,
                "scores": scores,
                "flagged_for_review": False,
                "review_history": review_history,
            }

        logger.warning(f"[{gate_name}] REJECTED attempt {attempt}: {feedback}")
        best_content = content

    # After max attempts — fail closed so bad content never proceeds.
    logger.error(
        f"[{gate_name}] MAX ATTEMPTS ({max_attempts}) reached. "
        f"Failing review gate."
    )
    result = {
        "approved": False,
        "content": best_content,
        "attempts": max_attempts,
        "feedback": last_feedback.get("feedback", "") if isinstance(last_feedback, dict) else last_feedback,
        "scores": last_scores,
        "flagged_for_review": True,
        "review_history": review_history,
    }
    raise ReviewGateError(gate_name, result)
