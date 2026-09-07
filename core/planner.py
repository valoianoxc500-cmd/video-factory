"""Planning helpers for topic selection and topic-history rotation."""

from __future__ import annotations

import logging
import random

import clients
import prompts
from core.utils import (
    ChannelConfig,
    compute_sections_range,
    load_topic_history,
    save_topic_history,
    select_least_recent,
)

logger = logging.getLogger("video_factory")


async def plan_video(
    config: ChannelConfig,
    channel_slug: str,
    *,
    override_type: str | None = None,
    override_content_family: str | None = None,
    override_topic: str | None = None,
) -> dict:
    """Select a topic and video type for the next video."""
    history = load_topic_history(channel_slug)
    past_topics = [h.get("topic", "") for h in history]

    video_types = {
        name: vt.model_dump()
        for name, vt in config.video_types.items()
        if vt.enabled
    }

    # Video type rotation.
    type_names = list(video_types.keys())
    preferred_idx = select_least_recent(type_names, history, "video_type")
    preferred_type = type_names[preferred_idx] if type_names else ""

    # Compute sections range so the planner picks topics with matching item counts
    target = config.video.target_duration_minutes
    preferred_vt = config.video_types.get(preferred_type)
    sections_range = compute_sections_range(target, preferred_vt) if preferred_vt else [2, 4]
    business_strategy = config.business_strategy
    selected_family = None
    content_families = []
    preferred_content_family = ""
    if business_strategy:
        families_by_name = {
            family.name: family
            for family in business_strategy.content_families
        }
        if override_content_family:
            if override_content_family not in families_by_name:
                available = ", ".join(sorted(families_by_name))
                raise ValueError(
                    f"Unknown content_family override '{override_content_family}'. "
                    f"Available: {available}"
                )
            selected_family = families_by_name[override_content_family]
            logger.info(f"Content family overridden to: {selected_family.name}")
        else:
            selected_family = random.choice(business_strategy.content_families)
            logger.info(f"Content family selected randomly: {selected_family.name}")
        content_families = [
            {
                "name": selected_family.name,
                "planning_focus": selected_family.planning_focus,
                "lead_magnet": selected_family.lead_magnet,
                "low_ticket_offer": selected_family.low_ticket_offer,
            }
        ]
        preferred_content_family = selected_family.name

    prompt = prompts.topic_selection_prompt(
        channel_name=config.channel_name,
        niche_focus=config.niche.focus,
        audience=config.niche.audience,
        example_topics=config.niche.example_topics,
        avoid_topics=config.niche.avoid_topics,
        video_types=video_types,
        past_topics=past_topics,
        language=config.language,
        preferred_type=preferred_type,
        target_duration_minutes=target,
        sections_range=sections_range,
        content_families=content_families,
        preferred_content_family=preferred_content_family,
        requested_topic=(override_topic or "").strip(),
    )

    result = await clients.generate_json(
        prompt,
        system_instruction=prompts.topic_selection_system(),
        temperature=0.9,
        operation_label="planning_topic_selection",
    )

    if override_topic and override_topic.strip():
        result["topic"] = override_topic.strip()
        logger.info(f"Topic overridden to: {result['topic']}")

    if override_type and override_type in video_types:
        result["video_type"] = override_type
        logger.info(f"Video type overridden to: {override_type}")

    if business_strategy:
        chosen_family = result.get("content_family", "")
        expected_family = selected_family.name if selected_family else ""
        if chosen_family != expected_family:
            raise ValueError(
                f"Planner selected content_family '{chosen_family}', "
                f"but expected '{expected_family}'."
            )
        family = selected_family
        result["content_family"] = family.name
        result["lead_magnet"] = family.lead_magnet
        result["low_ticket_offer"] = family.low_ticket_offer
        result["mid_ticket_offer"] = business_strategy.mid_ticket_offer
        result["later_offers"] = business_strategy.later_offers
        result["channel_goal"] = business_strategy.channel_goal
        result["video_jobs"] = business_strategy.video_jobs
        result["cta_rules"] = business_strategy.cta_rules
        result["cta_angle"] = family.cta_angle
        result["launch_strategy"] = business_strategy.launch_strategy
        logger.info(f"Content family: {family.name}")
        logger.info(f"Lead magnet: {family.lead_magnet}")
        logger.info(f"Low-ticket offer: {family.low_ticket_offer}")

    logger.info(f"Topic selected: {result.get('topic', '???')}")
    logger.info(f"Video type: {result.get('video_type', '???')}")
    logger.info(f"Angle: {result.get('angle', '???')}")

    # Metadata diversification.
    yt = config.youtube
    fmt_names = [f.name for f in yt.title_formats]
    idx = select_least_recent(fmt_names, history, "title_format")
    result["title_format"] = yt.title_formats[idx].name
    result["title_format_instruction"] = yt.title_formats[idx].instruction
    logger.info(f"Title format: {result['title_format']}")

    style_names = [s.name for s in yt.description_styles]
    idx = select_least_recent(style_names, history, "description_style")
    result["description_style"] = yt.description_styles[idx].name
    result["description_style_instruction"] = yt.description_styles[idx].instruction
    logger.info(f"Description style: {result['description_style']}")

    # Narrative hook rotation.
    vt_name = result.get("video_type", "")
    vt_config = config.video_types.get(vt_name)
    hooks = vt_config.narrative_hooks if vt_config else []
    if hooks:
        hook_names = [str(i) for i in range(len(hooks))]
        hook_idx = select_least_recent(hook_names, history, "narrative_hook_idx")
        result["narrative_hook"] = hooks[hook_idx]
        result["narrative_hook_idx"] = str(hook_idx)
        logger.info(f"Narrative hook: [{hook_idx}] {hooks[hook_idx][:60]}...")
    else:
        result["narrative_hook"] = ""
        result["narrative_hook_idx"] = ""

    return result


def record_completed_video(
    channel_slug: str,
    topic: str,
    video_type: str,
    output_video: str,
    title_format: str,
    description_style: str,
    thumbnail_strategy: str,
    content_family: str = "",
    narrative_hook_idx: str = "",
) -> None:
    """Save completed final-review package to topic history."""
    history = load_topic_history(channel_slug)
    history.append({
        "topic": topic,
        "video_type": video_type,
        "output_video": output_video,
        "title_format": title_format,
        "description_style": description_style,
        "thumbnail_strategy": thumbnail_strategy,
        "content_family": content_family,
        "narrative_hook_idx": narrative_hook_idx,
    })
    save_topic_history(channel_slug, history)

