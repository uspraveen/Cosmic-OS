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
    return load_model_specs().get(normalized)


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
