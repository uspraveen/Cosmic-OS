from __future__ import annotations

from shared import estimate_text_tokens, get_model_spec, load_model_specs, lookup_model_spec


def test_model_specs_registry_contains_active_runtime_models() -> None:
    specs = load_model_specs()
    assert "anthropic:claude-haiku-4-5" in specs
    assert "anthropic:claude-opus-4-6" in specs
    assert "perplexity:sonar" in specs
    assert "groq:openai/gpt-oss-20b" in specs
    assert "openai:gpt-5-mini" in specs
    assert "openai:gpt-image-1.5" in specs
    assert "xai:grok-imagine-image-pro" in specs


def test_lookup_model_spec_returns_expected_context_metadata() -> None:
    haiku = lookup_model_spec("anthropic", "claude-haiku-4-5")
    assert haiku is not None
    assert haiku.context_window_tokens == 200000
    assert haiku.recommended_headroom_reserve_tokens == 8000

    classifier = get_model_spec("groq:openai/gpt-oss-20b")
    assert classifier is not None
    assert classifier.base_url == "https://api.groq.com/openai/v1"

    gpt5mini = get_model_spec("openai:gpt-5-mini")
    assert gpt5mini is not None
    assert gpt5mini.pricing["input_per_1m_usd"] == 0.25
    assert gpt5mini.pricing["cached_input_per_1m_usd"] == 0.025
    assert gpt5mini.pricing["output_per_1m_usd"] == 2.0

    gpt_image = get_model_spec("openai:gpt-image-1.5")
    assert gpt_image is not None
    assert gpt_image.pricing["output_per_1m_usd"] == 32.0
    assert gpt_image.pricing["generation_per_image_usd"]["high"]["1024x1536"] == 0.2

    grok_image = get_model_spec("xai:grok-imagine-image-pro")
    assert grok_image is not None
    assert grok_image.pricing["output_image_each_usd"] == 0.07


def test_estimate_text_tokens_is_bounded_and_nonzero_for_text() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("hello") >= 1
    assert estimate_text_tokens("a" * 400) >= 100
