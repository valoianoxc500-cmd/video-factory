"""Tests for image sourcing candidate selection."""

import asyncio
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from prompts import (
    image_review_prompt,
    pexels_candidate_selection_prompt,
)
from core import image_sourcer, reviewer
from core.utils import ChannelConfig, Script, ScriptSection, VisualSlot


class _FakeResponse:
    def __init__(self, *, json_data=None, content: bytes = b"", status_code: int = 200):
        self._json_data = json_data
        self.content = content
        self.status_code = status_code
        self.text = ""

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


class _FakeClient:
    def __init__(self, images: dict[str, bytes]):
        self.images = images
        self.search_params = None
        # A beat searches a ladder of queries now, not one, so the whole grid
        # is recorded rather than just the last call.
        self.searches: list[dict] = []

    async def get(self, url: str, **kwargs):
        if url == "https://api.pexels.com/v1/search":
            self.search_params = kwargs.get("params")
            self.searches.append(kwargs.get("params") or {})
            return _FakeResponse(
                json_data={
                    "photos": [
                        {"src": {"large2x": "https://img.test/first.jpg"}},
                        {"src": {"large2x": "https://img.test/second.jpg"}},
                    ]
                }
            )
        return _FakeResponse(content=self.images[url])


class _FakeSerperClient:
    def __init__(self, images: dict[str, bytes]):
        self.images = images
        self.search_body = None

    async def post(self, url: str, **kwargs):
        assert url == "https://google.serper.dev/images"
        self.search_body = kwargs.get("json")
        return _FakeResponse(
            json_data={
                "images": [
                    {
                        "imageUrl": "https://img.test/first.jpg",
                        "source": "https://example.com/first",
                    },
                    {
                        "imageUrl": "https://img.test/second.jpg",
                        "source": "https://example.com/second",
                    },
                ]
            }
        )

    async def get(self, url: str, **kwargs):
        return _FakeResponse(content=self.images[url])


class _FakeVideoClient:
    def __init__(self):
        self.requested_url = None
        self.requested_params = None
        self.searches: list[dict] = []

    async def get(self, url: str, **kwargs):
        self.requested_url = url
        self.requested_params = kwargs.get("params")
        self.searches.append(kwargs.get("params") or {})
        return _FakeResponse(json_data={"videos": []})


class _FakeVideoErrorClient:
    async def get(self, url: str, **kwargs):
        request = httpx.Request("GET", url, params=kwargs.get("params"))
        return httpx.Response(403, request=request, text="blocked")


def _jpg_bytes(color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 9), color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _jpg_bytes_size(size: tuple[int, int], color: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="JPEG")
    return buffer.getvalue()


def _write_jpg(path, color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1920, 1080), color).save(path, format="JPEG")


def test_search_pexels_uses_vision_selected_candidate(monkeypatch, tmp_path):
    first = _jpg_bytes((255, 0, 0))
    second = _jpg_bytes((0, 255, 0))
    client = _FakeClient({
        "https://img.test/first.jpg": first,
        "https://img.test/second.jpg": second,
    })
    review_calls = []

    async def review_with_vision(prompt, image_paths, **kwargs):
        review_calls.append((prompt, list(image_paths)))
        assert len(image_paths) == 2
        assert all(path.exists() for path in image_paths)
        return {
            "approved": True,
            "winner_index": 2,
            "reason": "Second image shows the requested seated knee extension",
        }

    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", review_with_vision)

    output_path = tmp_path / "section_002_01.jpg"
    seen_hashes: set[str] = set()
    result = asyncio.run(
        image_sourcer._search_pexels(
            "senior man seated knee extension chair exercise",
            "Senior man sitting upright in a chair extending one leg",
            output_path,
            client,
            seen_hashes,
            target_size=(16, 9),
        )
    )

    assert result is True
    assert output_path.read_bytes() == second
    queried = [search["query"] for search in client.searches]
    # The beat's own brief leads the ladder, and every rung is searched in
    # both orientations.
    assert queried[0] == "senior man seated knee extension chair exercise"
    assert {search["orientation"] for search in client.searches} == {
        "portrait", "landscape",
    }
    assert all(
        search["per_page"] == image_sourcer._PEXELS_PER_QUERY_PAGE
        for search in client.searches
    )
    assert len(review_calls) == 1
    assert "body position and exercise type must match" in review_calls[0][0]
    assert len(seen_hashes) == 1


def test_download_valid_image_bytes_reframes_near_usable_small_source():
    image_bytes = _jpg_bytes_size((900, 900), (120, 160, 200))
    client = _FakeClient({"https://img.test/square.jpg": image_bytes})

    result = asyncio.run(
        image_sourcer._download_valid_image_bytes(
            client,
            "https://img.test/square.jpg",
            (1920, 1080),
            "square.jpg",
        )
    )

    assert result is not None
    with Image.open(io.BytesIO(result)) as img:
        assert img.size == (1920, 1080)


def test_download_valid_image_bytes_still_rejects_tiny_source():
    image_bytes = _jpg_bytes_size((300, 300), (120, 160, 200))
    client = _FakeClient({"https://img.test/tiny.jpg": image_bytes})

    result = asyncio.run(
        image_sourcer._download_valid_image_bytes(
            client,
            "https://img.test/tiny.jpg",
            (1920, 1080),
            "tiny.jpg",
        )
    )

    assert result is None


def test_search_serper_uses_vision_selected_candidate(monkeypatch, tmp_path):
    first = _jpg_bytes((255, 0, 0))
    second = _jpg_bytes((0, 255, 0))
    client = _FakeSerperClient({
        "https://img.test/first.jpg": first,
        "https://img.test/second.jpg": second,
    })
    review_calls = []

    async def review_with_vision(prompt, image_paths, **kwargs):
        review_calls.append((prompt, list(image_paths), kwargs.get("operation_label")))
        return {
            "approved": True,
            "winner_index": 2,
            "reason": "Second image shows the requested seated torso twist.",
        }

    monkeypatch.setattr(image_sourcer.settings, "serper_api_key", "key")
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", review_with_vision)

    output_path = tmp_path / "section_002_01.jpg"
    seen_hashes: set[str] = set()
    result = asyncio.run(
        image_sourcer._search_serper(
            "senior seated torso twist chair exercise",
            "Senior seated upright in a chair performing a torso twist stretch",
            output_path,
            client,
            seen_hashes,
            target_size=(16, 9),
        )
    )

    assert result is True
    assert output_path.read_bytes() == second
    assert client.search_body == {
        "q": "senior seated torso twist chair exercise",
        "num": 10,
        "imageType": "photo",
    }
    assert review_calls[0][2] == "serper_candidate_selection"
    assert "body position and exercise type must match" in review_calls[0][0]
    assert len(review_calls[0][1]) == 2
    assert len(seen_hashes) == 1


def test_search_pexels_rejects_candidate_set(monkeypatch, tmp_path):
    client = _FakeClient({
        "https://img.test/first.jpg": _jpg_bytes((255, 0, 0)),
        "https://img.test/second.jpg": _jpg_bytes((0, 255, 0)),
    })

    async def review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": False,
            "reason": "All candidates show the wrong exercise",
        }

    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", review_with_vision)

    output_path = tmp_path / "section_002_01.jpg"
    seen_hashes: set[str] = set()
    result = asyncio.run(
        image_sourcer._search_pexels(
            "senior man seated knee extension chair exercise",
            "Senior man sitting upright in a chair extending one leg",
            output_path,
            client,
            seen_hashes,
            target_size=(16, 9),
        )
    )

    assert result is False
    assert not output_path.exists()
    assert seen_hashes == set()


def test_search_pexels_skips_failed_candidate_download(monkeypatch, tmp_path):
    second = _jpg_bytes((0, 255, 0))
    client = _FakeClient({"https://img.test/second.jpg": second})
    reviewed_counts = []

    async def review_with_vision(prompt, image_paths, **kwargs):
        reviewed_counts.append(len(image_paths))
        return {
            "approved": True,
            "winner_index": 1,
            "reason": "Only valid candidate matches",
        }

    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", review_with_vision)

    output_path = tmp_path / "section_002_01.jpg"
    result = asyncio.run(
        image_sourcer._search_pexels(
            "senior man seated knee extension chair exercise",
            "Senior man sitting upright in a chair extending one leg",
            output_path,
            client,
            set(),
            target_size=(16, 9),
        )
    )

    assert result is True
    assert output_path.read_bytes() == second
    assert reviewed_counts == [1]


def test_search_pexels_video_uses_documented_v1_endpoint(monkeypatch, tmp_path):
    client = _FakeVideoClient()
    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")

    result = asyncio.run(
        image_sourcer._search_pexels_video(
            keywords="senior standing up from bed",
            output_path=tmp_path / "section_001_01.mp4",
            client=client,
            seen_hashes=set(),
            target_duration=5.0,
            target_size=(1280, 720),
            fps=30,
        )
    )

    assert result is False
    assert client.requested_url == "https://api.pexels.com/v1/videos/search"

    # The video path searches the same query ladder the photo path uses, in
    # both orientations, so there is no single request to assert on. What this
    # test still owns is the shape of each one: the documented endpoint above,
    # the brief leading the ladder, and a page size per request.
    ladder = image_sourcer.pexels_query_ladder("senior standing up from bed")
    assert ladder[0] == "senior standing up from bed"
    assert [search["query"] for search in client.searches] == [
        query for query in ladder for _ in ("portrait", "landscape")
    ]
    assert {search["orientation"] for search in client.searches} == {
        "portrait", "landscape",
    }
    assert {search["per_page"] for search in client.searches} == {5}


def test_search_pexels_video_returns_false_on_http_error(monkeypatch, tmp_path):
    monkeypatch.setattr(image_sourcer.settings, "pexels_api_key", "key")

    result = asyncio.run(
        image_sourcer._search_pexels_video(
            keywords="senior standing up from bed",
            output_path=tmp_path / "section_001_01.mp4",
            client=_FakeVideoErrorClient(),
            seen_hashes=set(),
            target_duration=5.0,
            target_size=(1280, 720),
            fps=30,
        )
    )

    assert result is False


def test_build_browser_safe_broll_encode_cmd_uses_libx264_faststart(tmp_path):
    cmd = image_sourcer._build_browser_safe_broll_encode_cmd(
        input_path=tmp_path / "input.mp4",
        output_path=tmp_path / "output.mp4",
        target_duration=5.0,
        target_size=(1280, 720),
        fps=30,
    )

    assert "libx264" in cmd
    assert "h264_nvenc" not in cmd
    assert "+faststart" in cmd
    assert "yuv420p" in cmd
    assert "fast" in cmd
    assert "18" in cmd


def test_image_review_prompt_requires_exact_exercise_broll():
    prompt = image_review_prompt([
        {
            "section_id": 3,
            "sub_image_index": 1,
            "narration": "Finally, exercise number three: Seated Marching.",
            "visual_type": "b_roll",
            "prompt": "",
            "image_search_keywords": "active senior woman doing seated marching exercise in chair",
            "image_filename": "section_003_01.jpg",
            "is_b_roll": True,
        }
    ])

    assert "For exercise, pose, physical therapy, or how-to movement B-roll" in prompt
    assert "seated marching" in prompt
    assert "generic seniors, chairs, talking heads" in prompt
    assert "Visual type: b_roll" in prompt


def test_image_review_prompt_includes_target_prompt_and_anatomy_rules():
    prompt = image_review_prompt([
        {
            "section_id": 2,
            "sub_image_index": 3,
            "narration": "Sit in a sturdy chair, keep both heels down, and lift your toes.",
            "visual_type": "ai_illustration",
            "prompt": "Full body illustration of a senior man sitting upright in a sturdy dining chair, both heels planted on the floor, with the toes of both feet actively lifted toward the ceiling.",
            "image_search_keywords": "",
            "image_filename": "section_002_03.jpg",
            "is_b_roll": False,
        }
    ])

    assert "Target prompt:" in prompt
    assert "both heels planted on the floor" in prompt
    assert "extra limbs, extra feet/toes, duplicated shoes" in prompt
    assert "wrong pose/body mechanics" in prompt
    assert "For photo-style warning-sign, symptom, or treatment scenes, be looser" in prompt
    assert "treat small hand/finger oddities as warning-level" in prompt
    assert "ai_illustration" in prompt


def test_image_review_prompt_uses_tagged_sections_and_failure_types():
    prompt = image_review_prompt([
        {
            "section_id": 2,
            "sub_image_index": 3,
            "narration": "Sit in a sturdy chair, keep both heels down, and lift your toes.",
            "visual_type": "ai_illustration",
            "prompt": "Full body illustration of a senior man sitting upright in a sturdy dining chair, both heels planted on the floor, with the toes of both feet actively lifted toward the ceiling.",
            "image_search_keywords": "",
            "image_filename": "section_002_03.jpg",
            "is_b_roll": False,
        }
    ])

    assert "<task>" in prompt
    assert "<rules>" in prompt
    assert "<examples>" in prompt
    assert "<schema>" in prompt
    assert "<input>" in prompt
    assert (
        '"failure_type": "wrong_subject" | "pose_mismatch" | "anatomy_error" '
        '| "weak_match" | "conflicting_branding"'
    ) in prompt
    assert "When approved is false, failure_type is REQUIRED." in prompt
    assert "Section opener: no" in prompt
    assert "Text-only component: no" in prompt
    assert "internal prompt-preview diagnostic slide" in prompt
    assert "Each element in image_results must be one flat single-image object." in prompt
    assert "Do not put top-level fields like approved, image_results, feedback, or scores" in prompt


def test_image_review_prompt_rejects_setting_only_matches_for_named_objects_and_actions():
    prompt = image_review_prompt([
        {
            "section_id": 5,
            "sub_image_index": 2,
            "narration": "Before you stand, try a few toe wiggles under the covers and warm the knee with a heating pad.",
            "visual_type": "stock_photo",
            "prompt": "",
            "image_search_keywords": "senior toe wiggles under bed covers heating pad on knee",
            "image_filename": "section_005_02.jpg",
            "is_b_roll": False,
        }
    ])

    assert "heating pad on stiff joints" in prompt
    assert "gift box" in prompt
    assert "same broad category but still misses the named object, tool, symptom, or action" in prompt
    assert "matching only the" in prompt.lower()
    assert "general lifestyle category is NOT enough".lower() in prompt.lower()


def test_pexels_candidate_selection_prompt_rejects_setting_only_matches():
    prompt = pexels_candidate_selection_prompt(
        keywords="senior toe wiggles under bed covers",
        prompt="Senior wiggling toes under the bed covers before standing up",
        num_images=3,
    )

    assert "same general category" in prompt
    assert "toe wiggles" in prompt
    assert "pillow under the knees" in prompt


def test_generation_style_uses_illustration_suffix_for_illustration_slots():
    config = ChannelConfig(
        channel_name="test",
        niche={
            "category": "test",
            "focus": "test",
            "audience": "general",
            "content_style": "informative",
        },
        youtube={
            "tags": ["health"],
            "title_formats": [{"name": "question", "instruction": "question"}],
            "description_styles": [{"name": "short", "instruction": "short"}],
        },
        image_sourcing={
            "generation_model": "gemini-test",
            "style_prompt_suffix": "photo suffix",
            "illustration_style_prompt_suffix": "illustration suffix",
        },
        thumbnail_strategies=[{"name": "hero", "instruction": "hero"}],
        thumbnail_presets=[{"background_source": "nano_banana"}],
    )

    assert image_sourcer._generation_style(config, True) == "illustration suffix"
    assert image_sourcer._generation_style(config, False) == "photo suffix"


def test_visual_slot_policy_names_are_explicit():
    assert {
        "source_as_written",
        "photo_backed_info_slide",
        "google_photo_exact_action",
        "literal_google_photo",
        "single_pose_ai_photo",
    } <= VisualSlot.VISUAL_POLICIES


def _image_source_config() -> ChannelConfig:
    return ChannelConfig(
        channel_name="Test",
        niche={
            "category": "test",
            "focus": "test",
            "audience": "general",
            "content_style": "informative",
        },
        youtube={
            "tags": ["health"],
            "title_formats": [{"name": "question", "instruction": "question"}],
            "description_styles": [{"name": "short", "instruction": "short"}],
        },
        image_sourcing={
            "generation_model": "gemini-test",
            "style_prompt_suffix": "photo suffix",
            "illustration_style_prompt_suffix": "illustration suffix",
        },
        review_thresholds={"image_review_max_attempts": 2},
        thumbnail_strategies=[{"name": "hero", "instruction": "hero"}],
    )


def _instructional_script() -> Script:
    return Script(
        title="Toe taps",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Sit in a sturdy chair, keep both heels down, and lift your toes toward the ceiling.",
                slots=[
                    VisualSlot(
                        visual="ai_illustration",
                        prompt="Full body illustration of a senior man sitting upright in a sturdy dining chair, both heels planted on the floor, with the toes of both feet actively lifted toward the ceiling.",
                        visual_policy="single_pose_ai_photo",
                    )
                ],
            )
        ],
    )


def _high_risk_followup_script() -> Script:
    return Script(
        title="Chair circulation",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Start with seated marches, then keep both heels down and lift your toes slowly.",
                slots=[
                    VisualSlot(
                        visual="comparison_bars",
                        props={
                            "title": "Setup",
                            "items": [
                                {"label": "Sit tall", "value": 1},
                                {"label": "Move slowly", "value": 1},
                            ],
                        },
                    ),
                    VisualSlot(
                        visual="ai_illustration",
                        prompt="Full body illustration of a senior sitting upright in a sturdy chair doing ankle pumps with both heels planted and toes lifting upward.",
                        visual_policy="single_pose_ai_photo",
                    ),
                ],
            )
        ],
    )


def _movement_info_slide_illustration_script() -> Script:
    return Script(
        title="Morning stiffness",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Before getting out of bed, do the ankle pump slowly to wake the joint back up.",
                slots=[
                    VisualSlot(
                        visual="title_banner",
                        overlay=True,
                        props={"title": "Ankle Pumps"},
                    ),
                    VisualSlot(
                        visual="info_slide",
                        prompt="A simple cross-section illustration of an ankle joint showing fluid moving through cartilage.",
                        visual_policy="photo_backed_info_slide",
                        props={
                            "title": "Why It Helps",
                            "text": "Gentle motion helps wake the joint back up.",
                        },
                    )
                ],
            )
        ],
    )


def _movement_support_ai_photo_script() -> Script:
    return Script(
        title="Morning stiffness",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Before standing, do gentle ankle pumps. Overnight, joint fluid thickens and circulation slows, which can make the knee feel stiff before you stand.",
                slots=[
                    VisualSlot(
                        visual="stock_photo",
                        prompt="Older adult doing ankle pumps in bed before standing.",
                        keywords="senior ankle pumps in bed real photo",
                    ),
                    VisualSlot(
                        visual="ai_photo",
                        prompt="Clean medical realistic photo of a human knee joint showing synovial fluid lubrication inside the capsule.",
                    ),
                ],
            )
        ],
    )


def _upper_body_instructional_script() -> Script:
    return Script(
        title="Gentle twists",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Try a seated torso twist by keeping your hips facing forward and gently rotating your upper body.",
                slots=[
                    VisualSlot(
                        visual="ai_illustration",
                        prompt="Instructional illustration of a senior doing a seated torso twist in a chair.",
                        visual_policy="single_pose_ai_photo",
                    )
                ],
            )
        ],
    )


def _warm_towel_script() -> Script:
    return Script(
        title="Warm towel",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Wrap a warm towel around the knee and rest it there for a few minutes before walking.",
                slots=[
                    VisualSlot(
                        visual="stock_photo",
                        prompt="Warm damp white towel wrapped securely around the knee of an older adult at home.",
                        keywords="warm towel knee",
                        visual_policy="literal_google_photo",
                    )
                ],
            )
        ],
    )


def _movement_stock_photo_opener_script() -> Script:
    return Script(
        title="Morning back stiffness",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Sit tall and try a gentle seated torso twist, keeping your hips facing forward.",
                slots=[
                    VisualSlot(
                        visual="stock_photo",
                        prompt="Senior woman seated upright doing a gentle torso twist in a chair.",
                        keywords="senior seated torso twist chair real photo",
                        visual_policy="google_photo_exact_action",
                    )
                ],
            )
        ],
    )


def _support_contact_broll_script() -> Script:
    return Script(
        title="Balance check",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="If you feel unsteady, walk slowly while lightly touching the sofa for balance.",
                slots=[
                    VisualSlot(
                        visual="b_roll",
                        prompt="Older adult walking while lightly touching a sofa for balance.",
                        keywords="senior walking near sofa balance",
                        visual_policy="google_photo_exact_action",
                    )
                ],
            )
        ],
    )


def _photo_script() -> Script:
    return Script(
        title="Morning stiffness",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="An older adult wakes up stiff and rubs their lower back before getting moving.",
                slots=[
                    VisualSlot(
                        visual="stock_photo",
                        prompt="Older adult waking up in bed with mild lower-back stiffness, soft morning light.",
                        keywords="senior waking up stiff bed back pain",
                    )
                ],
            )
        ],
    )


def test_source_images_fails_on_anatomy_rejection(monkeypatch, tmp_path):
    review_prompts = []
    generate_calls = []
    script = Script(
        title="Sleep posture",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Place a pillow under your knees while lying on your back to reduce lower-back strain.",
                slots=[
                        VisualSlot(
                            visual="ai_illustration",
                            prompt="Illustration of proper lying posture with a pillow under the knees and relaxed lower back.",
                            keywords="back sleeper pillow under knees sleep posture",
                            visual_policy="single_pose_ai_photo",
                        )
                ],
            )
        ],
    )

    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        generate_calls.append({
            "prompt": prompt,
            "model": kwargs.get("model"),
            "operation_label": kwargs.get("operation_label"),
            "aspect_ratio": kwargs.get("aspect_ratio"),
            "image_size": kwargs.get("image_size"),
        })
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (80, 120, 160)).save(output_path, format="JPEG")
        return output_path

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        review_prompts.append(prompt)
        assert len(image_paths) == 1
        assert image_paths[0].exists()
        return [
            {
                "section_id": 1,
                "sub_image_index": 1,
                "severity": "error",
                "failure_type": "anatomy_error",
                "issues": [
                    "extra feet",
                    "pose does not keep both heels planted",
                ],
                "suggestion": "Regenerate with correct foot anatomy and both heels planted.",
            }
        ]

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)
    async def fake_search_serper(keywords, prompt, output_path, client, seen_hashes, target_size):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (120, 140, 160)).save(output_path, format="JPEG")
        return True

    monkeypatch.setattr(image_sourcer, "_search_serper", fake_search_serper)

    with pytest.raises(reviewer.ReviewGateError):
        asyncio.run(
            image_sourcer.source_images(
                script,
                _image_source_config(),
                tmp_path,
            )
        )

    # The gate re-sources the rejected slot and reviews again before failing
    # closed. An image rejected for broken anatomy is regenerated, so there is
    # one generation per attempt. Tied to the configured budget rather than a
    # literal so the two cannot drift apart.
    attempts = _image_source_config().review_thresholds.image_review_max_attempts
    assert len(review_prompts) == attempts
    assert len(generate_calls) == attempts
    assert generate_calls[0]["model"] == "gemini-test"
    assert generate_calls[0]["operation_label"] == "image_generate"
    assert generate_calls[0]["aspect_ratio"] == "16:9"
    assert generate_calls[0]["image_size"] == "1K"
    assert "Target prompt:" in review_prompts[0]
    assert "pillow under the knees" in review_prompts[0]


def test_source_images_forces_support_contact_scene_to_google_photo(monkeypatch, tmp_path):
    source_calls = []

    async def fake_source_single_image(*, keywords, prompt, image_source, output_path, **kwargs):
        source_calls.append({
            "keywords": keywords,
            "prompt": prompt,
            "image_source": image_source,
        })
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (100, 120, 140)).save(output_path, format="JPEG")
        return "serper"

    async def fake_review_with_vision(*args, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Looks good.",
        }

    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)

    script = _support_contact_broll_script()
    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    slot = script.sections[0].slots[0]
    assert result["approved"] is True
    assert slot.visual == "google_photo"
    assert source_calls[0]["image_source"] == "serper"
    assert source_calls[0]["keywords"] == "senior walking near sofa balance"
    assert source_calls[0]["prompt"] == "Older adult walking while lightly touching a sofa for balance."


def test_source_images_preview_ai_prompts_skips_generation(monkeypatch, tmp_path):
    config = _image_source_config()
    config.test.preview_ai_image_prompts = True

    async def fail_generate_image_gemini(*args, **kwargs):
        raise AssertionError("test prompt preview must not call image generation")

    async def fake_generate_json(prompt, **kwargs):
        return {"approved": True, "feedback": "No sourced images to review."}

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Looks good.",
        }

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fail_generate_image_gemini)
    monkeypatch.setattr(image_sourcer.clients, "generate_json", fake_generate_json)

    script = Script(
        title="Prompt preview",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Show a calm older adult doing a simple chair movement.",
                slots=[
                    VisualSlot(
                        visual="ai_photo",
                        prompt="Older adult seated in a chair lifting one knee.",
                        keywords="senior seated knee lift exercise",
                    )
                ],
            )
        ],
    )

    result = asyncio.run(image_sourcer.source_images(script, config, tmp_path))

    slot = script.sections[0].slots[0]
    assert result["approved"] is True
    assert result["sourcing_log"][0]["source"] == "ai_prompt_preview"
    assert result["sourcing_log"][0]["model"] == "gemini-test"
    assert slot.visual == "text_only_slide"
    assert "Model: gemini-test" in slot.props["text"]
    assert "Operation: image_generate" in slot.props["text"]
    assert "Older adult seated in a chair lifting one knee" in slot.props["text"]
    assert not list((tmp_path / "images" / "raw").glob("section_*"))


def test_source_images_preview_ai_prompts_turns_locked_source_miss_into_slide(monkeypatch, tmp_path):
    config = _image_source_config()
    config.test.preview_ai_image_prompts = True
    source_calls = []

    async def fake_source_single_image(**kwargs):
        source_calls.append(kwargs)
        return None

    async def fake_generate_json(prompt, **kwargs):
        return {"approved": True, "feedback": "No sourced images to review."}

    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)
    monkeypatch.setattr(image_sourcer.clients, "generate_json", fake_generate_json)

    script = Script(
        title="Prompt preview",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Cold toes can be a warning sign.",
                slots=[
                    VisualSlot(
                        visual="info_slide",
                        prompt="Older adult in bed checking cold toes.",
                        keywords="older adult checking cold toes in bed real photo",
                        visual_policy="photo_backed_info_slide",
                        props={"title": "Cold Toes", "text": "Check the pattern."},
                    )
                ],
            )
        ],
    )

    result = asyncio.run(image_sourcer.source_images(script, config, tmp_path))

    slot = script.sections[0].slots[0]
    assert result["approved"] is True
    assert source_calls[0]["allow_generation_fallback"] is False
    assert result["sourcing_log"][0]["source"] == "ai_prompt_preview"
    assert result["sourcing_log"][0]["model"] == "(none; serper source)"
    assert slot.visual == "text_only_slide"
    assert slot.props["operation"] == "image_source_preview"
    assert "Older adult in bed checking cold toes." in slot.props["prompt_text"]
    assert "older adult checking cold toes in bed real photo" in slot.props["prompt_text"]


def test_source_images_fails_failed_exercise_demo_without_rescue(monkeypatch, tmp_path):
    review_prompts = []
    generate_calls = []
    serper_calls = []

    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        generate_calls.append({
            "prompt": prompt,
            "operation_label": kwargs.get("operation_label"),
        })
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (80, 120, 160)).save(output_path, format="JPEG")
        return output_path

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        review_prompts.append(prompt)
        return [
            {
                "section_id": 1,
                "sub_image_index": 1,
                "severity": "error",
                "failure_type": "pose_mismatch",
                "issues": ["heel should stay down"],
                "suggestion": "Teach ankle pumps with the heels planted and toes lifting upward.",
            }
        ]

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)
    async def fake_search_serper(keywords, prompt, output_path, client, seen_hashes, target_size):
        serper_calls.append((keywords, prompt))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (90, 130, 170)).save(output_path, format="JPEG")
        return True

    monkeypatch.setattr(image_sourcer, "_search_serper", fake_search_serper)
    monkeypatch.setattr(
        image_sourcer.clients,
        "generate_json",
        lambda *args, **kwargs: asyncio.sleep(0, result={"approved": True, "feedback": "No sourced images to review."}),
    )

    script = _instructional_script()
    with pytest.raises(reviewer.ReviewGateError):
        asyncio.run(
            image_sourcer.source_images(
                script,
                _image_source_config(),
                tmp_path,
            )
        )

    # Re-sourced and re-reviewed once before failing closed. The slot is an
    # ai_photo, so the retry regenerates rather than reaching for the web --
    # serper stays untouched.
    attempts = _image_source_config().review_thresholds.image_review_max_attempts
    assert len(generate_calls) == attempts
    assert len(review_prompts) == attempts
    assert serper_calls == []
    slot = script.sections[0].slots[0]
    assert slot.visual == "ai_photo"
    assert slot.prompt != ""


def test_high_risk_ai_illustration_opener_rewrites_to_ai_photo(monkeypatch, tmp_path):
    generate_calls = []

    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        generate_calls.append({
            "prompt": prompt,
            "model": kwargs.get("model"),
            "operation_label": kwargs.get("operation_label"),
        })
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (120, 140, 160)).save(output_path, format="JPEG")
        return output_path

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Movement demo is acceptable.",
        }

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)

    script = _instructional_script()
    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    assert result["approved"] is True
    assert len(generate_calls) == 1
    assert script.sections[0].slots[0].visual == "ai_photo"
    assert script.sections[0].slots[0].keywords == ""
    assert script.sections[0].slots[0].prompt.startswith("Full body illustration")
    assert result["sourcing_log"][0]["source"] == "ai_gen"


def test_high_risk_ai_illustration_followup_rewrites_to_ai_photo(monkeypatch, tmp_path):
    generate_calls = []
    saved_scripts = []

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 2,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "test review passed",
        }

    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        generate_calls.append({
            "prompt": prompt,
            "model": kwargs.get("model"),
            "operation_label": kwargs.get("operation_label"),
        })
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (120, 140, 160)).save(output_path, format="JPEG")
        return output_path

    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)
    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini)
    monkeypatch.setattr(image_sourcer, "save_script", lambda ws, script: saved_scripts.append((ws, script)))

    script = _high_risk_followup_script()
    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    assert result["approved"] is True
    assert len(generate_calls) == 1
    assert script.sections[0].slots[1].visual == "ai_photo"
    assert script.sections[0].slots[1].keywords == ""
    assert script.sections[0].slots[1].prompt.startswith("Full body illustration")
    assert len(saved_scripts) == 1
    assert any(entry["source"] == "ai_gen" for entry in result["sourcing_log"])


def test_source_images_saves_mutated_script_when_image_review_raises(monkeypatch, tmp_path):
    saved_scripts = []

    async def fake_review_gate(**kwargs):
        raise reviewer.ReviewGateError(
            "image_review",
            {
                "approved": False,
                "attempts": 2,
                "feedback": "still wrong",
                "flagged_for_review": True,
                "review_history": [],
            },
        )

    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (120, 140, 160)).save(output_path, format="JPEG")
        return output_path

    monkeypatch.setattr(image_sourcer, "review_gate", fake_review_gate)
    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini)
    monkeypatch.setattr(image_sourcer, "save_script", lambda ws, script: saved_scripts.append((ws, script)))

    script = _high_risk_followup_script()

    with pytest.raises(reviewer.ReviewGateError):
        asyncio.run(
            image_sourcer.source_images(
                script,
                _image_source_config(),
                tmp_path,
            )
        )

    assert script.sections[0].slots[1].visual == "ai_photo"
    assert len(saved_scripts) == 1


def test_movement_info_slide_opener_is_not_forced_text_only(monkeypatch, tmp_path):
    source_calls = []

    async def fake_source_single_image(**kwargs):
        source_calls.append(kwargs)
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (90, 110, 130)).save(kwargs["output_path"], format="JPEG")
        return "serper"

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Opener info slide is acceptable.",
        }

    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)

    script = _movement_info_slide_illustration_script()
    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    assert result["approved"] is True
    assert len(source_calls) == 2
    slot = script.sections[0].slots[1]
    assert slot.visual == "info_slide"
    assert slot.prompt != ""
    assert slot.props.get("source_as_photo") is True
    assert [entry["source"] for entry in result["sourcing_log"]] == ["serper", "serper"]


def test_movement_support_ai_photo_stays_visual_support(monkeypatch, tmp_path):
    async def fail_generate_image_gemini(*args, **kwargs):
        raise AssertionError("Movement support explainer should not generate anatomy art")

    source_calls = []

    async def fake_source_single_image(**kwargs):
        source_calls.append(kwargs)
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (120, 140, 160)).save(kwargs["output_path"], format="JPEG")
        return "pexels"

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Movement opener is acceptable.",
        }

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fail_generate_image_gemini)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)
    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)

    script = _movement_support_ai_photo_script()
    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    assert result["approved"] is True
    assert script.sections[0].slots[1].visual == "ai_photo"
    assert source_calls[1]["image_source"] == "ai_gen"
    assert source_calls[1]["prompt"] == script.sections[0].slots[1].prompt


def test_movement_info_slide_demo_prompt_is_not_forced_text_only(monkeypatch, tmp_path):
    source_calls = []

async def fake_source_single_image(**kwargs):
    source_calls.append(kwargs)
    kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1920, 1080), (100, 120, 140)).save(kwargs["output_path"], format="JPEG")
    return "serper"

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Demo visual is acceptable.",
        }

    script = Script(
        title="Ankle pumps",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Sit tall and do ankle pumps before standing.",
                slots=[
                    VisualSlot(
                        visual="info_slide",
                        prompt="Realistic photo of an older adult seated on a chair performing ankle pumps.",
                        props={"title": "Ankle Pumps", "text": "Lift your toes slowly."},
                    )
                ],
            )
        ],
    )

    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)
    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)

    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    assert result["approved"] is True
    assert script.sections[0].slots[0].visual == "info_slide"
    assert script.sections[0].slots[0].prompt != ""
    assert script.sections[0].slots[0].props.get("source_as_photo") is True
    assert script.sections[0].slots[0].props.get("photo_source") == "google_photo"
    assert source_calls[0]["lane"] == "photo"
    assert source_calls[0]["image_source"] == "serper"
    assert "Realistic photo of an older adult performing" in source_calls[0]["prompt"]


def test_movement_support_info_slide_uses_photo_lane_not_illustration(monkeypatch, tmp_path):
    source_calls = []

async def fake_source_single_image(**kwargs):
    source_calls.append(kwargs)
    kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1920, 1080), (110, 130, 150)).save(kwargs["output_path"], format="JPEG")
    return "serper"

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Support visual is acceptable.",
        }

    script = Script(
        title="Calf cramps",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Try a seated calf stretch before bed. Stretching before bed helps prevent overnight calf shortening.",
                slots=[
                    VisualSlot(
                        visual="title_banner",
                        overlay=True,
                        props={"title": "Seated Calf Stretch"},
                    ),
                    VisualSlot(
                        visual="info_slide",
                        prompt="Diagram of a lengthened calf muscle with a green checkmark",
                        props={"title": "Prevents Muscle Shortening", "text": "Stretching before bed helps."},
                    ),
                ],
            )
        ],
    )

    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)
    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)

    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    slot = script.sections[0].non_overlay_slots[0]
    assert result["approved"] is True
    assert slot.visual == "info_slide"
    assert slot.props.get("source_as_photo") is True
    assert slot.props.get("photo_source") == "google_photo"
    assert source_calls[0]["lane"] == "photo"
    assert source_calls[0]["image_source"] == "serper"
    assert "No arrows, labels, anatomy overlays" in source_calls[0]["prompt"]


def test_condition_info_slide_uses_photo_lane_not_illustration(monkeypatch, tmp_path):
    source_calls = []

    async def fake_source_single_image(**kwargs):
        source_calls.append(kwargs)
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (120, 140, 150)).save(kwargs["output_path"], format="JPEG")
        return "serper"

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Support photo is acceptable.",
        }

    script = Script(
        title="Cold feet",
        video_type="listicle",
        content_family="workflow_story",
        sections=[
            ScriptSection(
                id=1,
                narration="Cold toes under heavy blankets can point to reduced circulation.",
                slots=[
                        VisualSlot(
                            visual="info_slide",
                            prompt="Older adult in bed with blankets and visible cold feet real photo",
                            visual_policy="photo_backed_info_slide",
                            props={"title": "Ice-Cold Toes", "text": "Cold toes can be a warning sign."},
                        ),
                ],
            )
        ],
    )

    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)
    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)

    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    slot = script.sections[0].non_overlay_slots[0]
    assert result["approved"] is True
    assert slot.visual == "info_slide"
    assert slot.props.get("source_as_photo") is True
    assert slot.props.get("photo_source") == "google_photo"
    assert source_calls[0]["lane"] == "photo"
    assert source_calls[0]["image_source"] == "serper"
    assert source_calls[0]["prompt"] == "Older adult in bed with blankets and visible cold feet real photo"


def test_condition_info_slide_photo_miss_falls_back_to_component(monkeypatch, tmp_path):
    source_calls = []

    async def fake_source_single_image(**kwargs):
        source_calls.append(kwargs)
        return None

    script = Script(
        title="Cold feet",
        video_type="listicle",
        content_family="workflow_story",
        sections=[
            ScriptSection(
                id=1,
                narration="Cold toes under heavy blankets can point to reduced circulation.",
                slots=[
                        VisualSlot(
                            visual="info_slide",
                            prompt="Older adult in bed with blankets and visible cold feet real photo",
                            visual_policy="photo_backed_info_slide",
                            props={"title": "Ice-Cold Toes"},
                        ),
                ],
            )
        ],
    )

    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)
    async def fake_review_gate(**kwargs):
        return {
            "approved": True,
            "content": None,
            "attempts": 1,
            "feedback": None,
            "scores": None,
            "flagged_for_review": False,
            "review_history": [],
        }

    monkeypatch.setattr(image_sourcer, "review_gate", fake_review_gate)

    with pytest.raises(RuntimeError, match="source failed"):
        asyncio.run(
            image_sourcer.source_images(
                script,
                _image_source_config(),
                tmp_path,
            )
        )

    slot = script.sections[0].non_overlay_slots[0]
    assert slot.visual == "info_slide"
    assert slot.prompt != ""
    assert source_calls[0]["image_source"] == "serper"


def test_all_movement_ai_illustration_openers_use_ai_photo(monkeypatch, tmp_path):
    generate_calls = []

    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        generate_calls.append({
            "prompt": prompt,
            "model": kwargs.get("model"),
            "operation_label": kwargs.get("operation_label"),
        })
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (100, 100, 120)).save(output_path, format="JPEG")
        return output_path

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Movement opener is acceptable.",
        }

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)

    script = _upper_body_instructional_script()
    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    assert result["approved"] is True
    assert len(generate_calls) == 1
    assert script.sections[0].slots[0].visual == "ai_photo"
    assert script.sections[0].slots[0].prompt.startswith("Instructional illustration")
    assert result["sourcing_log"][0]["source"] == "ai_gen"


def test_movement_stock_photo_opener_stays_as_real_demo(monkeypatch, tmp_path):
    source_calls = []

    async def fake_source_single_image(**kwargs):
        source_calls.append({
            "image_source": kwargs["image_source"],
            "lane": kwargs["lane"],
        })
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (120, 140, 160)).save(kwargs["output_path"], format="JPEG")
        return "serper"

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Movement opener is acceptable.",
        }

    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)

    script = _movement_stock_photo_opener_script()
    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    assert result["approved"] is True
    assert script.sections[0].slots[0].visual == "google_photo"
    assert source_calls == [{"image_source": "serper", "lane": "photo"}]
    assert result["sourcing_log"][0]["source"] == "serper"


def test_warm_towel_opener_stays_photo_only(monkeypatch, tmp_path):
    generate_calls = []

    async def fake_search_serper(*args, **kwargs):
        return False

    async def fake_generate_json(prompt, **kwargs):
        return {"approved": True, "feedback": "test review passed"}

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "warning",
                    "issues": ["accepted for unit test"],
                    "suggestion": "",
                }
            ],
            "feedback": "Literal photo-only lane stayed out of AI generation.",
        }

    async def fail_generate_image_gemini(*args, **kwargs):
        generate_calls.append(kwargs)
        raise AssertionError("Warm towel close-up should not fall back to AI generation")

    monkeypatch.setattr(image_sourcer, "_search_serper", fake_search_serper)
    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fail_generate_image_gemini)
    monkeypatch.setattr(image_sourcer.clients, "generate_json", fake_generate_json)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)

    script = _warm_towel_script()
    with pytest.raises(RuntimeError, match="source failed"):
        asyncio.run(
            image_sourcer.source_images(
                script,
                _image_source_config(),
                tmp_path,
            )
        )

    assert generate_calls == []
    assert script.sections[0].slots[0].visual == "google_photo"


def test_source_images_rejected_result_fails_review_gate(monkeypatch, tmp_path):
    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (80, 120, 160)).save(output_path, format="JPEG")
        return output_path

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return [
            {
                "section_id": 1,
                "sub_image_index": 1,
                "severity": "error",
                "issues": ["wrong subject"],
                "suggestion": "Use the correct waking-up scene.",
            }
        ]

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)

    with pytest.raises(reviewer.ReviewGateError):
        asyncio.run(
            image_sourcer.source_images(
                _photo_script(),
                _image_source_config(),
                tmp_path,
            )
        )


def test_broll_fallback_uses_illustration_for_anatomy_sensitive_visuals(monkeypatch, tmp_path):
    generate_calls = []

    script = Script(
        title="Toe wiggles",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Before you stand up, wiggle your toes under the covers for a few seconds.",
                slots=[
                    VisualSlot(
                        visual="b_roll",
                        keywords="senior wiggling toes under the bed covers before standing",
                    )
                ],
            )
        ],
    )

    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        generate_calls.append({
            "prompt": prompt,
            "model": kwargs.get("model"),
            "operation_label": kwargs.get("operation_label"),
            "aspect_ratio": kwargs.get("aspect_ratio"),
            "image_size": kwargs.get("image_size"),
        })
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (90, 110, 140)).save(output_path, format="JPEG")
        return output_path

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Looks good.",
        }

    async def fake_search_pexels_video(**kwargs):
        return False

    async def fake_search_pexels(*args, **kwargs):
        return False

    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)
    monkeypatch.setattr(image_sourcer, "_search_pexels_video", fake_search_pexels_video)
    monkeypatch.setattr(image_sourcer, "_search_pexels", fake_search_pexels)

    result = asyncio.run(
        image_sourcer.source_images(
            script,
            _image_source_config(),
            tmp_path,
        )
    )

    assert result["approved"] is True
    assert len(generate_calls) == 1
    assert generate_calls[0]["model"] == "gemini-test"
    assert generate_calls[0]["operation_label"] == "image_generate"
    assert generate_calls[0]["aspect_ratio"] == "16:9"
    assert generate_calls[0]["image_size"] == "1K"
    assert "illustration" not in generate_calls[0]["prompt"].lower()


def test_source_images_requires_info_slide_media_even_when_generation_flag_disabled(monkeypatch, tmp_path):
    config = _image_source_config()
    config.image_sourcing.generate_info_slide_illustrations = False
    generate_calls = []
    script = Script(
        title="Safety cue",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Sit on the edge of the bed before standing.",
                slots=[
                    VisualSlot(
                        visual="info_slide",
                        prompt="Illustration of a senior sitting on the edge of the bed before standing.",
                        props={"title": "Safe Standing", "text": "Pause before standing."},
                    )
                ],
            )
        ],
    )

    async def fake_generate_json(prompt, **kwargs):
        return {"approved": True, "feedback": "No sourced images to review."}

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": True,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": True,
                    "severity": "ok",
                    "issues": [],
                    "suggestion": "",
                }
            ],
            "feedback": "Looks good.",
        }

    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        generate_calls.append({"prompt": prompt, "model": kwargs.get("model")})
        output_path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (120, 140, 160)).save(output_path, format="JPEG")
        return output_path

    monkeypatch.setattr(image_sourcer.clients, "generate_json", fake_generate_json)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)
    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini)

    result = asyncio.run(image_sourcer.source_images(script, config, tmp_path))

    assert result["approved"] is True
    assert result["attempts"] == 1
    assert result["sourcing_log"][0]["source"] == "ai_gen"
    assert generate_calls[0]["model"] == "gemini-test"


def test_source_images_blank_info_slide_fails_loudly(monkeypatch, tmp_path):
    source_calls = []

    async def fake_source_single_image(**kwargs):
        source_calls.append(kwargs)
        return "ai_gen"

    async def fake_generate_json(prompt, **kwargs):
        return {"approved": True, "feedback": "No sourced images to review."}

    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)
    monkeypatch.setattr(image_sourcer.clients, "generate_json", fake_generate_json)

    script = Script(
        title="Cold feet",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Cold feet can be an early sign of circulation issues.",
                slots=[
                    VisualSlot(
                        visual="info_slide",
                        prompt="empty",
                        props={"title": "What It Means", "text": "Watch for one foot being colder."},
                    )
                ],
            )
        ],
    )

    with pytest.raises(ValueError, match="info_slide requires an image prompt"):
        asyncio.run(image_sourcer.source_images(script, _image_source_config(), tmp_path))

    assert source_calls == []


def test_source_single_image_serper_miss_does_not_fallback_to_generation(monkeypatch, tmp_path):
    async def fake_search_serper(*args, **kwargs):
        return False

    async def fail_generate_image_gemini(*args, **kwargs):
        raise AssertionError("AI generation fallback should not run after Serper miss")

    monkeypatch.setattr(image_sourcer, "_search_serper", fake_search_serper)
    monkeypatch.setattr(image_sourcer.clients, "generate_image_gemini", fail_generate_image_gemini)

    result = asyncio.run(
        image_sourcer._source_single_image(
            keywords="older adult checking cold foot real photo",
            prompt="Real photo of older adult checking cold foot",
            image_source="serper",
            config=_image_source_config(),
            output_path=tmp_path / "section_001_01.jpg",
            client=_FakeSerperClient({}),
            seen_hashes=set(),
            lane="photo",
            allow_generation_fallback=False,
        )
    )

    assert result is None


def test_image_review_rejection_keeps_previous_asset_and_fails_gate(monkeypatch, tmp_path):
    review_paths: list[list[str]] = []

    async def fake_source_single_image(**kwargs):
        kwargs["output_path"].parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (1920, 1080), (90, 110, 130)).save(kwargs["output_path"], format="JPEG")
        return "serper"

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        review_paths.append([path.name for path in image_paths])
        return {
            "approved": False,
            "image_results": [
                {
                    "section_id": 1,
                    "sub_image_index": 1,
                    "approved": False,
                    "failure_type": "weak_match",
                    "severity": "error",
                    "issues": ["wrong action"],
                    "suggestion": "Use a different real photo",
                }
            ],
            "feedback": "Need a better photo.",
        }

    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)

    script = Script(
        title="Balance warning",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="If one foot feels colder, check it while seated.",
                slots=[
                    VisualSlot(
                        visual="google_photo",
                        keywords="older adult checking cold foot real photo",
                        prompt="Real photo of older adult checking a cold foot while seated.",
                    )
                ],
            )
        ],
    )

    with pytest.raises(reviewer.ReviewGateError):
        asyncio.run(image_sourcer.source_images(script, _image_source_config(), tmp_path))

    raw_image = tmp_path / "images" / "raw" / "section_001_01.jpg"
    assert raw_image.exists()
    # Every pass reviews the same slot: the rejected file is re-sourced in
    # place, so the asset kept on disk stays section_001_01.jpg throughout.
    attempts = _image_source_config().review_thresholds.image_review_max_attempts
    assert review_paths == [["section_001_01.jpg"]] * attempts


@pytest.mark.parametrize(
    "suggestion,expected",
    [
        # The schema asks the reviewer to quote the query it wants.
        ("Search for 'Anfield stadium crowd' instead", "Anfield stadium crowd"),
        ('Use "Alexander Isak Liverpool debut" instead', "Alexander Isak Liverpool debut"),
        # Unquoted prose: strip the instruction wrapper, keep the subject.
        ("Find a photograph of Portman Road", "Portman Road"),
        ("Use a real photo of Alexander Isak celebrating", "Alexander Isak celebrating"),
        # Already a bare query -- leave it alone.
        ("Anfield stadium crowd photograph", "Anfield stadium crowd photograph"),
        # Vague prose is not a query; fall back to the normal tiers.
        ("Try to source an image that better represents the specific moment "
         "described in the narration segment above.", ""),
        ("", ""),
    ],
)
def test_query_from_suggestion(suggestion, expected):
    """The reviewer's suggestion is prose for a human, not a search query.

    Passing it verbatim searched for the sentence rather than the subject: a
    rejected slot was re-queried with "Search for a high-quality image of a
    ticking clock face...", which matched worse than the query that had
    already failed.
    """
    assert image_sourcer._query_from_suggestion(suggestion) == expected


def test_cached_images_are_still_reachable_by_the_review_gate(monkeypatch, tmp_path):
    """An image already on disk must still be re-sourceable when rejected.

    On a resumed run every image is cached, so the sourcing pass built no
    descriptors at all. The review gate then had nothing to re-source: it
    re-reviewed byte-identical files until the retry budget ran out and failed
    the run, with no way for the operator to make progress.
    """
    source_calls: list[str] = []

    async def fake_source_single_image(**kwargs):
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        source_calls.append(out.name)
        shade = 40 + 30 * len(source_calls)
        Image.new("RGB", (1920, 1080), (shade, shade, shade)).save(out, format="JPEG")
        return "serper"

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": False,
            "image_results": [{
                "section_id": 1,
                "sub_image_index": 1,
                "approved": False,
                "failure_type": "wrong_subject",
                "severity": "error",
                "issues": ["infographic, not a photo"],
                "suggestion": "Anfield stadium crowd photograph",
            }],
            "feedback": "Image 1.1 is a subject mismatch.",
        }

    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)

    # Pre-place the image, exactly as a resumed run would find it.
    raw_dir = tmp_path / "images" / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (1920, 1080), (10, 10, 10)).save(
        raw_dir / "section_001_01.jpg", format="JPEG"
    )

    script = Script(
        title="Deadline day",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="Anfield reacted.",
                slots=[VisualSlot(visual="google_photo", keywords="Anfield crowd",
                                  prompt="Stadium crowd.")],
            )
        ],
    )

    with pytest.raises(reviewer.ReviewGateError):
        asyncio.run(image_sourcer.source_images(script, _image_source_config(), tmp_path))

    assert source_calls, (
        "a cached image rejected by the review gate was never re-sourced; "
        "the gate had no descriptor to act on"
    )


def test_rejected_filenames_survive_slot_renumbering():
    """Reviewer indices must resolve through filenames, not descriptor order.

    Reproduces the renumbering case: section 3's first slot went unsourced and
    was dropped, so the survivors were renumbered and the file the reviewer
    calls "3.2" is section_003_03.jpg on disk. Index-based matching resolved
    this to nothing, so the gate re-reviewed identical images until its retry
    budget ran out and then failed the run.
    """
    sections_context = [
        {"section_id": 3, "sub_image_index": 1, "image_filename": "section_003_02.jpg"},
        {"section_id": 3, "sub_image_index": 2, "image_filename": "section_003_03.jpg"},
    ]
    rejected = {(3, 2): "Anfield stadium crowd photograph"}

    resolved = image_sourcer._rejected_filenames(rejected, sections_context)

    assert resolved == {"section_003_03.jpg": "Anfield stadium crowd photograph"}


def test_rejected_filenames_ignores_unknown_keys():
    """A reviewer index with no matching context entry must not raise."""
    resolved = image_sourcer._rejected_filenames(
        {(9, 9): "something"},
        [{"section_id": 1, "sub_image_index": 1, "image_filename": "section_001_01.jpg"}],
    )
    assert resolved == {}


def test_image_review_regeneration_targets_the_rejected_slot(monkeypatch, tmp_path):
    """A rejected image must actually be re-sourced, not just re-reviewed.

    The reviewer numbers images the way sections_context lists them, which is
    assigned after unsourced slots are dropped and survivors renumbered -- so
    it does not track each descriptor's original sub_idx. Matching on that
    index resolved every rejection to zero targets: the gate burned its whole
    retry budget re-reviewing the identical files and then failed the run.
    Resolution goes through the filename, so this asserts a real re-source.
    """
    source_calls: list[str] = []

    async def fake_source_single_image(**kwargs):
        out = kwargs["output_path"]
        out.parent.mkdir(parents=True, exist_ok=True)
        source_calls.append(out.name)
        # Vary the pixels so the retry is not rejected as a duplicate hash.
        shade = 40 + 30 * len(source_calls)
        Image.new("RGB", (1920, 1080), (shade, shade, shade)).save(out, format="JPEG")
        return "serper"

    async def fake_review_with_vision(prompt, image_paths, **kwargs):
        return {
            "approved": False,
            "image_results": [
                {
                    "section_id": 2,
                    "sub_image_index": 1,
                    "approved": False,
                    "failure_type": "wrong_subject",
                    "severity": "error",
                    "issues": ["shows an infographic, not the stadium"],
                    "suggestion": "Anfield stadium crowd photograph",
                }
            ],
            "feedback": "Image 2.1 is a subject mismatch.",
        }

    monkeypatch.setattr(image_sourcer, "_source_single_image", fake_source_single_image)
    monkeypatch.setattr(image_sourcer.clients, "review_with_vision", fake_review_with_vision)

    script = Script(
        title="Deadline day",
        video_type="listicle",
        sections=[
            ScriptSection(
                id=1,
                narration="The window shut at eleven.",
                slots=[VisualSlot(visual="google_photo", keywords="transfer deadline clock",
                                  prompt="Deadline day clock.")],
            ),
            ScriptSection(
                id=2,
                narration="Anfield reacted.",
                slots=[VisualSlot(visual="google_photo", keywords="Anfield crowd",
                                  prompt="Stadium crowd.")],
            ),
        ],
    )

    with pytest.raises(reviewer.ReviewGateError):
        asyncio.run(image_sourcer.source_images(script, _image_source_config(), tmp_path))

    # Section 2's file is sourced once on the first pass and again on the
    # retry; section 1 was approved and must not be re-sourced.
    assert source_calls.count("section_002_01.jpg") == 2, (
        f"rejected slot was not re-sourced; calls={source_calls}"
    )
    assert source_calls.count("section_001_01.jpg") == 1, (
        f"approved slot should not be re-sourced; calls={source_calls}"
    )


def _web_photos_only_config() -> ChannelConfig:
    config = _image_source_config()
    config.image_sourcing.web_photos_only = True
    return config


def test_web_photos_only_never_falls_back_to_generation(monkeypatch, tmp_path):
    """A web-photos-only channel must never produce an AI-generated visual.

    Regression: b-roll descriptors omitted allow_generation_fallback, so the
    `.get(..., True)` default re-enabled image generation for those slots and
    an AI image reached the render.
    """
    generated: list[str] = []

    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        generated.append(str(output_path))
        return output_path

    async def fake_search_serper(*args, **kwargs):
        return False  # every web search misses

    monkeypatch.setattr(
        image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini
    )
    monkeypatch.setattr(image_sourcer, "_search_serper", fake_search_serper)

    result = asyncio.run(
        image_sourcer._source_single_image(
            keywords="stadium crowd",
            prompt="a stadium crowd",
            image_source="serper",
            config=_web_photos_only_config(),
            output_path=tmp_path / "section_001_02.jpg",
            client=object(),
            seen_hashes=set(),
            lane="photo",
            # The b-roll fallback path used to arrive here with True.
            allow_generation_fallback=True,
        )
    )

    assert result is None
    assert generated == [], "web_photos_only must not generate images"


def test_generation_still_allowed_when_web_photos_only_is_off(monkeypatch, tmp_path):
    """The guard must not disable generation for ordinary channels."""
    generated: list[str] = []

    async def fake_generate_image_gemini(prompt, output_path, **kwargs):
        generated.append(str(output_path))
        # Writes a file, as the real client does. Returning a path without one
        # is the failure mode `_is_usable_asset` exists to catch.
        Path(output_path).write_bytes(_jpg_bytes((10, 20, 30)))
        return output_path

    async def fake_search_serper(*args, **kwargs):
        return False

    monkeypatch.setattr(
        image_sourcer.clients, "generate_image_gemini", fake_generate_image_gemini
    )
    monkeypatch.setattr(image_sourcer, "_search_serper", fake_search_serper)

    result = asyncio.run(
        image_sourcer._source_single_image(
            keywords="stadium crowd",
            prompt="a stadium crowd",
            image_source="serper",
            config=_image_source_config(),
            output_path=tmp_path / "section_001_02.jpg",
            client=object(),
            seen_hashes=set(),
            lane="photo",
            allow_generation_fallback=True,
        )
    )

    assert result == "ai_gen"
    assert len(generated) == 1
