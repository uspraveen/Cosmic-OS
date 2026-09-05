from __future__ import annotations

from shared import estimate_text_tokens, get_model_spec, load_model_specs, lookup_model_spec


def test_model_specs_registry_contains_active_runtime_models() -> None:
    specs = load_model_specs()
    assert "anthropic:claude-haiku-4-5" in specs
    assert "anthropic:claude-opus-4-6" in specs
    assert "anthropic:claude-sonnet-4-6" in specs
    assert "perplexity:sonar" in specs
    assert "perplexity:pplx-embed-v1-4b" in specs
    assert "fireworks:accounts/fireworks/models/kimi-k2p6" in specs
    assert "fireworks:accounts/fireworks/models/glm-5p3" in specs
    assert "fireworks:accounts/fireworks/models/glm-5p3-flash" in specs
    assert "fireworks:accounts/fireworks/models/qwen3p6-plus" in specs
    assert "fireworks:accounts/fireworks/models/qwen3p7-plus" in specs
    assert "groq:openai/gpt-oss-20b" in specs
    assert "openai:gpt-5-mini" in specs
    assert "openai:gpt-5.6-sol" in specs
    assert "openai:gpt-5.6-terra" in specs
    assert "openai:gpt-5.6-luna" in specs
    assert "openai:gpt-image-1.5" in specs
    assert "xai:grok-imagine-image" in specs
    assert "xai:grok-imagine-image-pro" in specs


def test_lookup_model_spec_returns_expected_context_metadata() -> None:
    haiku = lookup_model_spec("anthropic", "claude-haiku-4-5")
    assert haiku is not None
    assert haiku.context_window_tokens == 200000
    assert haiku.recommended_headroom_reserve_tokens == 8000
    assert haiku.pricing["input_per_1m_usd"] == 1.0
    assert haiku.pricing["cached_input_per_1m_usd"] == 0.1
    assert haiku.pricing["output_per_1m_usd"] == 5.0

    opus = get_model_spec("anthropic:claude-opus-4-6")
    assert opus is not None
    assert opus.pricing["input_per_1m_usd"] == 5.0
    assert opus.pricing["cached_input_per_1m_usd"] == 0.5
    assert opus.pricing["output_per_1m_usd"] == 25.0

    sonnet = get_model_spec("anthropic:claude-sonnet-4-6")
    assert sonnet is not None
    assert sonnet.pricing["input_per_1m_usd"] == 3.0
    assert sonnet.pricing["cached_input_per_1m_usd"] == 0.3
    assert sonnet.pricing["output_per_1m_usd"] == 15.0

    sonar = get_model_spec("perplexity:sonar")
    assert sonar is not None
    assert sonar.pricing["input_per_1m_usd"] == 1.0
    assert sonar.pricing["output_per_1m_usd"] == 1.0

    classifier = get_model_spec("groq:openai/gpt-oss-20b")
    assert classifier is not None
    assert classifier.base_url == "https://api.groq.com/openai/v1"
    assert classifier.pricing["input_per_1m_usd"] == 0.075
    assert classifier.pricing["cached_input_per_1m_usd"] == 0.037
    assert classifier.pricing["output_per_1m_usd"] == 0.3

    gpt5mini = get_model_spec("openai:gpt-5-mini")
    assert gpt5mini is not None
    assert gpt5mini.pricing["input_per_1m_usd"] == 0.25
    assert gpt5mini.pricing["cached_input_per_1m_usd"] == 0.025
    assert gpt5mini.pricing["output_per_1m_usd"] == 2.0

    luna = get_model_spec("openai:gpt-5.6-luna")
    assert luna is not None
    assert luna.context_window_tokens == 1_050_000
    assert luna.max_output_tokens == 128_000
    assert luna.pricing["input_per_1m_usd"] == 0.2
    assert luna.pricing["cached_input_per_1m_usd"] == 0.02
    assert luna.pricing["output_per_1m_usd"] == 1.2
    assert luna.capabilities["supports_image_input"] is True
    assert luna.capabilities["supports_tool_calling"] is True
    assert "completion_tokens_details.reasoning_tokens" in luna.token_field_map["reasoning_tokens"]

    terra = get_model_spec("openai:gpt-5.6-terra")
    assert terra is not None
    assert terra.pricing["input_per_1m_usd"] == 2.0
    assert terra.pricing["cached_input_per_1m_usd"] == 0.2
    assert terra.pricing["output_per_1m_usd"] == 12.0

    sol = get_model_spec("openai:gpt-5.6-sol")
    assert sol is not None
    assert sol.pricing["input_per_1m_usd"] == 4.0
    assert sol.pricing["cached_input_per_1m_usd"] == 0.4
    assert sol.pricing["output_per_1m_usd"] == 20.0

    gpt_image = get_model_spec("openai:gpt-image-1.5")
    assert gpt_image is not None
    assert gpt_image.pricing["output_per_1m_usd"] == 32.0
    assert gpt_image.pricing["generation_per_image_usd"]["high"]["1024x1536"] == 0.2

    grok_image_std = get_model_spec("xai:grok-imagine-image")
    assert grok_image_std is not None
    assert grok_image_std.pricing["output_image_each_usd"] == 0.02

    grok_image = get_model_spec("xai:grok-imagine-image-pro")
    assert grok_image is not None
    assert grok_image.pricing["output_image_each_usd"] == 0.07

    pplx_embed = get_model_spec("perplexity:pplx-embed-v1-4b")
    assert pplx_embed is not None
    assert pplx_embed.pricing["input_per_1m_usd"] == 0.03

    kimi = get_model_spec("fireworks:accounts/fireworks/models/kimi-k2p6")
    assert kimi is not None
    assert kimi.sdk == "openai_compatible"
    assert kimi.context_window_tokens == 262000
    assert kimi.pricing["input_per_1m_usd"] == 0.95
    assert kimi.pricing["cached_input_per_1m_usd"] == 0.16
    assert kimi.pricing["output_per_1m_usd"] == 4.0
    assert kimi.capabilities["supports_image_input"] is True
    assert kimi.capabilities["supports_tool_calling"] is True
    assert "prompt_tokens_details.cached_tokens" in kimi.token_field_map["cached_tokens"]
    assert "completion_tokens_details.reasoning_tokens" in kimi.token_field_map["reasoning_tokens"]

    glm = get_model_spec("fireworks:accounts/fireworks/models/glm-5p3")
    assert glm is not None
    assert glm.sdk == "openai_compatible"
    assert glm.context_window_tokens == 1_040_000
    assert glm.max_output_tokens == 131_072
    assert glm.pricing["input_per_1m_usd"] == 1.4
    assert glm.pricing["cached_input_per_1m_usd"] == 0.26
    assert glm.pricing["output_per_1m_usd"] == 4.4
    assert glm.capabilities["supports_image_input"] is False
    assert glm.capabilities["supports_tool_calling"] is True

    glm_flash = get_model_spec("fireworks:accounts/fireworks/models/glm-5p3-flash")
    assert glm_flash is not None
    assert glm_flash.sdk == "openai_compatible"
    assert glm_flash.context_window_tokens == 1_040_000
    assert glm_flash.pricing["input_per_1m_usd"] == 0.15
    assert glm_flash.pricing["cached_input_per_1m_usd"] == 0.029
    assert glm_flash.pricing["output_per_1m_usd"] == 0.5
    # GLM 5.3 Flash is natively multimodal - image turns must stay on it.
    assert glm_flash.capabilities["supports_image_input"] is True
    assert glm_flash.capabilities["supports_tool_calling"] is True

    qwen = get_model_spec("fireworks:accounts/fireworks/models/qwen3p6-plus")
    assert qwen is not None
    assert qwen.context_window_tokens == 1_000_000
    assert qwen.pricing["input_per_1m_usd"] == 0.5
    assert qwen.pricing["output_per_1m_usd"] == 3.0
    assert qwen.capabilities["supports_image_input"] is True
    assert qwen.capabilities["supports_tool_calling"] is True

    qwen37 = get_model_spec("fireworks:accounts/fireworks/models/qwen3p7-plus")
    assert qwen37 is not None
    assert qwen37.status == "active"
    assert qwen37.pricing["input_per_1m_usd"] == 0.5
    assert qwen37.pricing["cached_input_per_1m_usd"] == 0.1
    assert qwen37.pricing["output_per_1m_usd"] == 3.0
    assert qwen37.capabilities["supports_image_input"] is True
    assert qwen37.capabilities["supports_reasoning_tokens"] is True


def test_estimate_text_tokens_is_bounded_and_nonzero_for_text() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("hello") >= 1
    assert estimate_text_tokens("a" * 400) >= 100
