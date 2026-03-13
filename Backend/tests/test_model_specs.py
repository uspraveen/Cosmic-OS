from __future__ import annotations

from shared import estimate_text_tokens, get_model_spec, load_model_specs, lookup_model_spec


def test_model_specs_registry_contains_active_runtime_models() -> None:
    specs = load_model_specs()
    assert "anthropic:claude-haiku-4-5" in specs
    assert "anthropic:claude-opus-4-6" in specs
    assert "perplexity:sonar" in specs
    assert "groq:openai/gpt-oss-20b" in specs


def test_lookup_model_spec_returns_expected_context_metadata() -> None:
    haiku = lookup_model_spec("anthropic", "claude-haiku-4-5")
    assert haiku is not None
    assert haiku.context_window_tokens == 200000
    assert haiku.recommended_headroom_reserve_tokens == 8000

    classifier = get_model_spec("groq:openai/gpt-oss-20b")
    assert classifier is not None
    assert classifier.base_url == "https://api.groq.com/openai/v1"


def test_estimate_text_tokens_is_bounded_and_nonzero_for_text() -> None:
    assert estimate_text_tokens("") == 0
    assert estimate_text_tokens("hello") >= 1
    assert estimate_text_tokens("a" * 400) >= 100
