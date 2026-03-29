from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from agents.image_generator_agent.agent import ImageGeneratorAgent, ProviderGenerationResult, ProviderImage, ReferenceImage
from agents.image_generator_agent.config import ImageGeneratorAgentConfig
from shared.contracts import TaskEnvelope, sign_task_envelope


class _FakeRedis:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []
        self._seq = 0

    async def incr(self, _key: str) -> int:
        self._seq += 1
        return self._seq

    async def xadd(self, _stream: str, fields: dict[str, object], **_kwargs) -> str:
        self.events.append((_stream, fields))
        return f"{len(self.events)}-0"

    async def rpush(self, _key: str, _value: str) -> int:
        return 1

    async def expire(self, _key: str, _ttl: int) -> bool:
        return True

    async def aclose(self) -> None:
        return None


def _task_for(
    intent: str,
    payload: dict[str, object],
    *,
    session_id: str = "sess_1",
    input_artifacts: list[dict[str, object]] | None = None,
) -> TaskEnvelope:
    base = TaskEnvelope(
        task_id="tsk_test_1" if intent == "image.generate" else "tsk_test_2",
        task_list_id="tl_1",
        parent_task_id=None,
        session_id=session_id,
        sender="cosmic/orchestrator:1.0.0",
        recipient="cosmic/image-generator-agent:1.0.0",
        intent=intent,
        input=payload,
        input_artifacts=input_artifacts or [],
        idempotency_key=f"idem_{intent}",
        priority="normal",
        signature="",
    )
    signature = sign_task_envelope(base, "test-secret")
    return base.model_copy(update={"signature": signature})


@pytest.mark.asyncio
async def test_image_generate_persists_artifacts_with_model_name(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = ImageGeneratorAgentConfig(
        gateway_internal_token="",
        openai_api_key="openai-key",
        xai_api_key="xai-key",
        enable_internal_router_llm=False,
    )
    agent = ImageGeneratorAgent(
        redis_client=_FakeRedis(),
        config=cfg,
        agent_secret="test-secret",
        artifacts_root=tmp_path / "runs" / "artifacts",
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
    )
    await agent.on_startup()

    async def fake_generate(*, task, normalized_input, route):
        return ProviderGenerationResult(
            provider=route.provider,
            model=route.model,
            request_payload={"prompt": normalized_input["prompt"]},
            response_payload={"id": "img_resp_1", "data": [{"b64_json": "<omitted>"}]},
            raw_usage={"images": 1},
            provider_request_id="img_resp_1",
            images=[
                ProviderImage(
                    data=_PNG_BYTES,
                    mime="image/png",
                    revised_prompt="A refined city skyline",
                    width=1,
                    height=1,
                )
            ],
        )

    monkeypatch.setattr(agent, "_generate_with_provider", fake_generate)

    result = await agent.execute(
        _task_for(
            "image.generate",
            {
                "prompt": "A cinematic neon city skyline at dusk",
                "artifact_basename": "city_skyline",
            },
        )
    )

    assert result.status == "completed"
    assert result.output["model"] == "grok-imagine-image-pro"
    assert len(result.artifacts) == 2
    deliverable = next(item for item in result.artifacts if item.audience == "deliverable")
    assert "grok-imagine-image-pro" in Path(deliverable.path).name
    assert result.output["images"][0]["filename"].endswith(".png")
    assert "grok-imagine-image-pro" in result.output["images"][0]["filename"]


@pytest.mark.asyncio
async def test_image_recall_session_reads_prior_runs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = ImageGeneratorAgentConfig(
        gateway_internal_token="",
        openai_api_key="openai-key",
        xai_api_key="xai-key",
        enable_internal_router_llm=False,
    )
    agent = ImageGeneratorAgent(
        redis_client=_FakeRedis(),
        config=cfg,
        agent_secret="test-secret",
        artifacts_root=tmp_path / "runs" / "artifacts",
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
    )
    await agent.on_startup()

    async def fake_generate(*, task, normalized_input, route):
        return ProviderGenerationResult(
            provider="openai",
            model="gpt-image-1.5",
            request_payload={"prompt": normalized_input["prompt"]},
            response_payload={"id": "img_resp_2", "data": [{"b64_json": "<omitted>"}]},
            raw_usage={"images": 1},
            provider_request_id="img_resp_2",
            images=[ProviderImage(data=_PNG_BYTES, mime="image/png", revised_prompt=None, width=1, height=1)],
        )

    monkeypatch.setattr(agent, "_generate_with_provider", fake_generate)
    await agent.execute(
        _task_for(
            "image.generate",
            {
                "prompt": "A poster with exact text labels and a structured layout",
                "prefer_model": "openai",
            },
            session_id="sess_recall",
        )
    )

    recall = await agent.execute(
        _task_for(
            "image.recall_session",
            {"session_id": "sess_recall", "limit": 5},
            session_id="sess_recall",
        )
    )

    assert recall.status == "completed"
    assert len(recall.output["entries"]) == 1
    assert recall.output["entries"][0]["model"] == "gpt-image-1.5"


@pytest.mark.asyncio
async def test_image_generate_accepts_reference_image_artifacts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cfg = ImageGeneratorAgentConfig(
        gateway_internal_token="",
        openai_api_key="openai-key",
        xai_api_key="xai-key",
        enable_internal_router_llm=False,
    )
    artifacts_root = tmp_path / "runs" / "artifacts"
    reference_path = artifacts_root / "tsk_prev" / "image_generator_agent" / "reference.png"
    reference_path.parent.mkdir(parents=True, exist_ok=True)
    reference_path.write_bytes(_PNG_BYTES)
    agent = ImageGeneratorAgent(
        redis_client=_FakeRedis(),
        config=cfg,
        agent_secret="test-secret",
        artifacts_root=artifacts_root,
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
    )
    await agent.on_startup()

    async def fake_generate(*, task, normalized_input, route, reference_images):
        assert task.intent == "image.generate"
        assert normalized_input["reference_image_count"] == 1
        assert route.provider == "openai"
        assert len(reference_images) == 1
        assert reference_images[0].artifact_ref["artifact_id"] == "art_ref_1"
        return ProviderGenerationResult(
            provider="openai",
            model="gpt-image-1.5",
            request_payload={"prompt": normalized_input["prompt"]},
            response_payload={"id": "img_edit_1", "data": [{"b64_json": "<omitted>"}]},
            raw_usage={"images": 1},
            provider_request_id="img_edit_1",
            images=[ProviderImage(data=_PNG_BYTES, mime="image/png", revised_prompt=None, width=1, height=1)],
        )

    monkeypatch.setattr(agent, "_generate_with_provider", fake_generate)

    result = await agent.execute(
        _task_for(
            "image.generate",
            {
                "prompt": "Turn this sphere render into a launch poster with typography.",
                "prefer_model": "openai",
                "artifact_basename": "launch_poster",
            },
            input_artifacts=[
                {
                    "artifact_id": "art_ref_1",
                    "mime": "image/png",
                    "path": "runs/artifacts/tsk_prev/image_generator_agent/reference.png",
                    "filename": "reference.png",
                }
            ],
        )
    )

    assert result.status == "completed"
    assert result.output["reference_images"] == [
        {
            "artifact_id": "art_ref_1",
            "path": "runs/artifacts/tsk_prev/image_generator_agent/reference.png",
            "mime": "image/png",
        }
    ]


_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.mark.asyncio
async def test_generate_via_image_api_uses_openai_images_generations_endpoint(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _FakeHttpClient:
        async def post(self, url, *, headers, json, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            captured["timeout"] = timeout
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                request=request,
                json={"id": "img_resp_openai", "usage": {"images": 1}, "data": [{"b64_json": base64_png()}]},
            )

    cfg = ImageGeneratorAgentConfig(
        gateway_internal_token="",
        openai_api_key="openai-key",
        xai_api_key="xai-key",
        enable_internal_router_llm=False,
    )
    agent = ImageGeneratorAgent(
        redis_client=_FakeRedis(),
        config=cfg,
        agent_secret="test-secret",
        artifacts_root=tmp_path / "runs" / "artifacts",
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        http_client=_FakeHttpClient(),
    )
    await agent.on_startup()

    result = await agent._generate_via_image_api(
        task=_task_for("image.generate", {"prompt": "Poster"}),  # noqa: SLF001
        provider="openai",
        model="gpt-image-1.5",
        api_key="openai-key",
        base_url="https://api.openai.com/v1",
        timeout_sec=60.0,
        normalized_input={
            "prompt": "Poster",
            "negative_prompt": None,
            "style_hint": None,
            "use_case": None,
            "complexity_hint": "complex",
            "prefer_model": "openai",
            "count": 1,
            "size": "1024x1536",
            "quality": "high",
            "artifact_basename": "poster",
        },
        route=type("Route", (), {"router_mode": "explicit"})(),
        reference_images=[],
    )

    assert captured["url"] == "https://api.openai.com/v1/images/generations"
    assert captured["json"]["size"] == "1024x1536"
    assert captured["json"]["quality"] == "high"
    assert "aspect_ratio" not in captured["json"]
    assert "response_format" not in captured["json"]
    assert result.model == "gpt-image-1.5"


@pytest.mark.asyncio
async def test_generate_via_image_api_uses_openai_images_edits_endpoint_for_reference_images(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _FakeHttpClient:
        async def post(self, url, *, headers, data=None, files=None, timeout, json=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["data"] = data
            captured["files"] = files
            captured["json"] = json
            captured["timeout"] = timeout
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                request=request,
                json={"id": "img_edit_openai", "usage": {"images": 1}, "data": [{"b64_json": base64_png()}]},
            )

    cfg = ImageGeneratorAgentConfig(
        gateway_internal_token="",
        openai_api_key="openai-key",
        xai_api_key="xai-key",
        enable_internal_router_llm=False,
    )
    agent = ImageGeneratorAgent(
        redis_client=_FakeRedis(),
        config=cfg,
        agent_secret="test-secret",
        artifacts_root=tmp_path / "runs" / "artifacts",
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        http_client=_FakeHttpClient(),
    )
    await agent.on_startup()

    result = await agent._generate_via_image_api(
        task=_task_for("image.generate", {"prompt": "Poster"}),  # noqa: SLF001
        provider="openai",
        model="gpt-image-1.5",
        api_key="openai-key",
        base_url="https://api.openai.com/v1",
        timeout_sec=60.0,
        normalized_input={
            "prompt": "Poster",
            "negative_prompt": None,
            "style_hint": None,
            "use_case": None,
            "complexity_hint": "complex",
            "prefer_model": "openai",
            "count": 1,
            "size": "1024x1536",
            "quality": "high",
            "artifact_basename": "poster",
        },
        route=type("Route", (), {"router_mode": "explicit"})(),
        reference_images=[
            ReferenceImage(
                artifact_ref={"artifact_id": "art_ref", "path": "runs/artifacts/ref.png", "mime": "image/png"},
                filename="ref.png",
                mime="image/png",
                data=_PNG_BYTES,
            )
        ],
    )

    assert captured["url"] == "https://api.openai.com/v1/images/edits"
    assert captured["json"] is None
    assert captured["data"]["input_fidelity"] == "high"
    assert captured["data"]["size"] == "1024x1536"
    assert captured["data"]["quality"] == "high"
    assert captured["files"][0][0] == "image[]"
    assert captured["files"][0][1][0] == "ref.png"
    assert captured["files"][0][1][2] == "image/png"
    assert result.model == "gpt-image-1.5"


@pytest.mark.asyncio
async def test_generate_via_image_api_maps_xai_size_to_aspect_ratio(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _FakeHttpClient:
        async def post(self, url, *, headers, json, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            captured["timeout"] = timeout
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                request=request,
                json={"id": "img_resp_xai", "usage": {"images": 1}, "data": [{"b64_json": base64_png()}]},
            )

    cfg = ImageGeneratorAgentConfig(
        gateway_internal_token="",
        openai_api_key="openai-key",
        xai_api_key="xai-key",
        enable_internal_router_llm=False,
    )
    agent = ImageGeneratorAgent(
        redis_client=_FakeRedis(),
        config=cfg,
        agent_secret="test-secret",
        artifacts_root=tmp_path / "runs" / "artifacts",
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        http_client=_FakeHttpClient(),
    )
    await agent.on_startup()

    result = await agent._generate_via_image_api(
        task=_task_for("image.generate", {"prompt": "Landscape"}),  # noqa: SLF001
        provider="xai",
        model="grok-imagine-image-pro",
        api_key="xai-key",
        base_url="https://api.x.ai/v1",
        timeout_sec=60.0,
        normalized_input={
            "prompt": "Landscape",
            "negative_prompt": None,
            "style_hint": None,
            "use_case": None,
            "complexity_hint": "auto",
            "prefer_model": "xai",
            "count": 1,
            "size": "1536x1024",
            "quality": "high",
            "artifact_basename": "landscape",
        },
        route=type("Route", (), {"router_mode": "heuristic"})(),
        reference_images=[],
    )

    assert captured["url"] == "https://api.x.ai/v1/images/generations"
    assert captured["json"]["aspect_ratio"] == "3:2"
    assert "size" not in captured["json"]
    assert "quality" not in captured["json"]
    assert captured["json"]["response_format"] == "b64_json"
    assert result.model == "grok-imagine-image-pro"


@pytest.mark.asyncio
async def test_generate_via_image_api_uses_xai_images_edits_endpoint_for_reference_image(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class _FakeHttpClient:
        async def post(self, url, *, headers, json, timeout):
            captured["url"] = url
            captured["headers"] = headers
            captured["json"] = json
            captured["timeout"] = timeout
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                request=request,
                json={"id": "img_edit_xai", "usage": {"images": 1}, "data": [{"b64_json": base64_png()}]},
            )

    cfg = ImageGeneratorAgentConfig(
        gateway_internal_token="",
        openai_api_key="openai-key",
        xai_api_key="xai-key",
        enable_internal_router_llm=False,
    )
    agent = ImageGeneratorAgent(
        redis_client=_FakeRedis(),
        config=cfg,
        agent_secret="test-secret",
        artifacts_root=tmp_path / "runs" / "artifacts",
        store_root=tmp_path / "store",
        runtime_root=tmp_path / "runtime",
        http_client=_FakeHttpClient(),
    )
    await agent.on_startup()

    result = await agent._generate_via_image_api(
        task=_task_for("image.generate", {"prompt": "Landscape"}),  # noqa: SLF001
        provider="xai",
        model="grok-imagine-image-pro",
        api_key="xai-key",
        base_url="https://api.x.ai/v1",
        timeout_sec=60.0,
        normalized_input={
            "prompt": "Landscape",
            "negative_prompt": None,
            "style_hint": None,
            "use_case": None,
            "complexity_hint": "auto",
            "prefer_model": "xai",
            "count": 1,
            "size": "1536x1024",
            "quality": "high",
            "artifact_basename": "landscape",
        },
        route=type("Route", (), {"router_mode": "heuristic"})(),
        reference_images=[
            ReferenceImage(
                artifact_ref={"artifact_id": "art_ref", "path": "runs/artifacts/ref.png", "mime": "image/png"},
                filename="ref.png",
                mime="image/png",
                data=_PNG_BYTES,
            )
        ],
    )

    assert captured["url"] == "https://api.x.ai/v1/images/edits"
    assert captured["json"]["image"]["type"] == "image_url"
    assert captured["json"]["image"]["url"].startswith("data:image/png;base64,")
    assert captured["json"]["response_format"] == "b64_json"
    assert "size" not in captured["json"]
    assert result.model == "grok-imagine-image-pro"


def base64_png() -> str:
    import base64

    return base64.b64encode(_PNG_BYTES).decode("ascii")
