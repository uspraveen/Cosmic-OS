from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from math import ceil
from pathlib import Path
from typing import Any


MODEL_SPECS_PATH = Path(__file__).with_name("model_specs.json")


@dataclass(frozen=True, slots=True)
class ModelSpec:
    key: str
    provider: str
    model: str
    sdk: str
    base_url: str
    usage_kind: str
    context_window_tokens: int | None
    max_output_tokens: int | None
    recommended_headroom_reserve_tokens: int
    pricing: dict[str, Any]
    capabilities: dict[str, Any]
    token_field_map: dict[str, list[str]]
    status: str


def build_model_key(provider: str, model: str) -> str:
    return f"{provider.strip().lower()}:{model.strip()}"


def estimate_text_tokens(text: str) -> int:
    normalized = str(text or "")
    if not normalized:
        return 0
    return max(1, ceil(len(normalized) / 4))


# Reasoning-effort levels accepted by the OpenAI chat-completions models, per
# family: GPT-5.6 adds xhigh/max; the earlier GPT-5 models (gpt-5, gpt-5-mini,
# ...) only accept up to high.
GPT5_6_REASONING_EFFORT_LEVELS = {"none", "low", "medium", "high", "xhigh", "max"}
GPT5_LEGACY_REASONING_EFFORT_LEVELS = {"minimal", "low", "medium", "high"}
DEFAULT_INTERNAL_REASONING_EFFORT = "xhigh"


def is_openai_gpt5_chat_model(model: str | None) -> bool:
    """True for GPT-5-family chat models, which reject temperature-style knobs."""
    return str(model or "").strip().casefold().startswith("gpt-5")


def infer_model_provider(base_url: str | None, model: str | None) -> str:
    """Best-effort vendor name for an OpenAI-compatible endpoint + model id.

    Usage events must carry a real vendor so model keys resolve against
    pricing cards; generic labels like "internal_llm" cannot be priced.
    """
    normalized = str(base_url or "").strip().lower()
    model_text = str(model or "").strip().lower()
    if "api.openai.com" in normalized or model_text.startswith("gpt-"):
        return "openai"
    if "fireworks.ai" in normalized:
        return "fireworks"
    if "api.x.ai" in normalized:
        return "xai"
    if "api.groq.com" in normalized:
        return "groq"
    if "api.anthropic.com" in normalized:
        return "anthropic"
    if "api.perplexity.ai" in normalized:
        return "perplexity"
    return "openai_compatible"


def normalized_reasoning_effort(
    model: str | None,
    requested: str | None = None,
    *,
    default: str = DEFAULT_INTERNAL_REASONING_EFFORT,
) -> str | None:
    """Resolve the reasoning_effort to send for a GPT-5-family chat model.

    Returns None for non-GPT-5 models (the parameter must not be sent) or when
    nothing valid can be resolved for the model's family. Empty/"auto" requests
    fall back to ``default``, clamped to the family's supported levels.
    """
    if not is_openai_gpt5_chat_model(model):
        return None
    name = str(model or "").strip().casefold()
    supported = GPT5_6_REASONING_EFFORT_LEVELS if "5.6" in name else GPT5_LEGACY_REASONING_EFFORT_LEVELS
    effort = str(requested or "").strip().casefold()
    if effort in {"", "auto", "default"}:
        effort = str(default or "").strip().casefold()
    if effort in supported:
        return effort
    return "high" if "high" in supported else None


@lru_cache(maxsize=1)
def load_model_specs() -> dict[str, ModelSpec]:
    payload = json.loads(MODEL_SPECS_PATH.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("shared/model_specs.json must contain a top-level object")
    raw_models = payload.get("models")
    if not isinstance(raw_models, dict):
        raise ValueError("shared/model_specs.json is missing the 'models' object")

    specs: dict[str, ModelSpec] = {}
    for key, value in raw_models.items():
        if not isinstance(value, dict):
            continue
        spec = ModelSpec(
            key=str(key),
            provider=str(value.get("provider") or "").strip(),
            model=str(value.get("model") or "").strip(),
            sdk=str(value.get("sdk") or "").strip(),
            base_url=str(value.get("base_url") or "").strip(),
            usage_kind=str(value.get("usage_kind") or "").strip(),
            context_window_tokens=_coerce_optional_int(value.get("context_window_tokens")),
            max_output_tokens=_coerce_optional_int(value.get("max_output_tokens")),
            recommended_headroom_reserve_tokens=max(
                0,
                _coerce_optional_int(value.get("recommended_headroom_reserve_tokens")) or 0,
            ),
            pricing=value.get("pricing") if isinstance(value.get("pricing"), dict) else {},
            capabilities=value.get("capabilities") if isinstance(value.get("capabilities"), dict) else {},
            token_field_map=_normalize_token_field_map(value.get("token_field_map")),
            status=str(value.get("status") or "active").strip() or "active",
        )
        specs[spec.key] = spec
    return specs


def get_model_spec(model_key: str) -> ModelSpec | None:
    normalized = str(model_key or "").strip()
    if not normalized:
        return None
    specs = load_model_specs()
    spec = specs.get(normalized)
    if spec is not None:
        return spec
    # Agent usage events label the internal LLM with generic provider names
    # ("internal_llm", "openai_compatible"). Resolve those to the real vendor
    # card so token mapping and cost estimation keep working; only when the
    # model name is unambiguous across the catalog.
    provider, separator, model = normalized.partition(":")
    if not separator or not model:
        return None
    if provider in {entry.provider for entry in specs.values()}:
        return None
    matches = [entry for entry in specs.values() if entry.model.casefold() == model.casefold()]
    return matches[0] if len(matches) == 1 else None


def lookup_model_spec(provider: str, model: str) -> ModelSpec | None:
    return get_model_spec(build_model_key(provider, model))


def _coerce_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _normalize_token_field_map(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, list[str]] = {}
    for key, item in value.items():
        if isinstance(item, list):
            normalized[str(key)] = [str(entry) for entry in item if str(entry).strip()]
        else:
            normalized[str(key)] = []
    return normalized
