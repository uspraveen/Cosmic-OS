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

    async def fake_generate(*, task, normalized_input, route, reference_images):
        assert reference_images == []
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

    async def fake_generate(*, task, normalized_input, route, reference_images):
        assert reference_images == []
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


@pytest.mark.asyncio
async def test_image_generate_accepts_goal_alias_for_prompt(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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

    async def fake_generate(*, task, normalized_input, route, reference_images):
        assert normalized_input["prompt"] == "Design an Apple-style launch poster for COSMIC."
        assert reference_images == []
        return ProviderGenerationResult(
            provider=route.provider,
            model=route.model,
            request_payload={"prompt": normalized_input["prompt"]},
            response_payload={"id": "img_goal_alias", "data": [{"b64_json": "<omitted>"}]},
            raw_usage={"images": 1},
            provider_request_id="img_goal_alias",
            images=[ProviderImage(data=_PNG_BYTES, mime="image/png", revised_prompt=None, width=1, height=1)],
        )

    monkeypatch.setattr(agent, "_generate_with_provider", fake_generate)

    result = await agent.execute(
        _task_for(
            "image.generate",
            {
                "goal": "Design an Apple-style launch poster for COSMIC.",
                "artifact_basename": "launch_poster",
            },
        )
    )

    assert result.status == "completed"
    assert result.output["images"][0]["filename"].startswith("launch_poster__")


_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc`\x00\x00"
    b"\x00\x02\x00\x01\xe2!\xbc3\x00\x00\x00\x00IEND\xaeB`\x82"
)

_JPEG_BYTES = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb00430008060607060508"
    "0707070909080a0c140d0c0b0b0c19120f131d1a1f1e1d1a1c1c20242e2720222c"
    "231c1c2837292c30313434341f27393d38323c2e333431ffdb0043010909090c0b"
    "0c180d0d1831201c20313131313131313131313131313131313131313131313131"
    "31313131313131313131313131313131313131313131ffc0001108000100010301"
    "2200021101031101ffc4001f000001050101010101010000000000000000010203"
    "0405060708090a0bffc400b5100002010303020403050504040000017d01020300"
    "041105122131410613516107227114328191a1082342b1c11552d1f02433627282"
    "090a161718191a25262728292a3435363738393a434445464748494a5354555657"
    "58595a636465666768696a737475767778797a838485868788898a929394959697"
    "98999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5"
    "d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101"
    "010101010101000000000000000102030405060708090a0bffc400b51100020102"
    "040403040705040400010277000102031104052131061241510761711322328108"
    "144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a3536"
    "3738393a434445464748494a535455565758595a636465666768696a7374757677"
    "78797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5"
    "b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3"
    "f4f5f6f7f8f9faffda000c03010002110311003f00fdfcffd9"
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


@pytest.mark.asyncio
async def test_generate_via_image_api_detects_actual_jpeg_mime(tmp_path: Path) -> None:
    class _FakeHttpClient:
        async def post(self, url, *, headers, json, timeout):
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                request=request,
                json={"id": "img_resp_xai_jpeg", "usage": {"images": 1}, "data": [{"b64_json": base64_jpeg()}]},
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
        task=_task_for("image.generate", {"prompt": "Portrait"}),  # noqa: SLF001
        provider="xai",
        model="grok-imagine-image-pro",
        api_key="xai-key",
        base_url="https://api.x.ai/v1",
        timeout_sec=60.0,
        normalized_input={
            "prompt": "Portrait",
            "negative_prompt": None,
            "style_hint": None,
            "use_case": None,
            "complexity_hint": "auto",
            "prefer_model": "xai",
            "count": 1,
            "size": "1024x1024",
            "quality": "high",
            "artifact_basename": "portrait",
        },
        route=type("Route", (), {"router_mode": "heuristic"})(),
        reference_images=[],
    )

    assert result.images[0].mime == "image/jpeg"


@pytest.mark.asyncio
async def test_persist_generated_images_uses_extension_for_detected_mime(tmp_path: Path) -> None:
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

    task = _task_for("image.generate", {"prompt": "Portrait"})
    generation = ProviderGenerationResult(
        provider="xai",
        model="grok-imagine-image-pro",
        request_payload={"prompt": "Portrait"},
        response_payload={"id": "img_resp_3", "data": [{"b64_json": "<omitted>"}]},
        raw_usage={"images": 1},
        provider_request_id="img_resp_3",
        images=[ProviderImage(data=_JPEG_BYTES, mime="image/jpeg", revised_prompt=None, width=1, height=1)],
    )

    artifacts, refs = agent._persist_generated_images(task=task, generation=generation, artifact_basename="portrait")

    deliverable = next(item for item in artifacts if item.audience == "deliverable")
    assert deliverable.mime == "image/jpeg"
    assert Path(deliverable.path).suffix == ".jpg"
    assert refs[0]["filename"].endswith(".jpg")


def base64_png() -> str:
    import base64

    return base64.b64encode(_PNG_BYTES).decode("ascii")


def base64_jpeg() -> str:
    import base64

    return base64.b64encode(_JPEG_BYTES).decode("ascii")
