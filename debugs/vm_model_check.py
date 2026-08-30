import sys

sys.path.insert(0, ".")
from dotenv import load_dotenv

load_dotenv("/etc/cosmic/orchestrator.env")
import os

os.environ.setdefault("ORCHESTRATOR_FIREWORKS_GLM_MODEL", os.getenv("ORCHESTRATOR_FIREWORKS_GLM_MODEL", ""))

from orchestrator.config import OrchestratorConfig

cfg = OrchestratorConfig.from_env()
print("glm model:", cfg.fireworks_glm_model)
print("kimi model:", cfg.fireworks_kimi_model)
print("vision fallback:", cfg.fireworks_vision_fallback_model)
print("default provider:", cfg.orchestrator_default_provider)

from shared import lookup_model_spec

for model in [cfg.fireworks_glm_model, "accounts/fireworks/models/glm-5p3-flash"]:
    spec = lookup_model_spec("fireworks", model)
    assert spec is not None, model
    print(
        spec.model,
        "| ctx:",
        spec.context_window_tokens,
        "| vision:",
        spec.capabilities.get("supports_image_input"),
        "| $in:",
        spec.pricing.get("input_per_1m_usd"),
    )

from gateway.preferences.store import GatewayPreferenceStore
from pathlib import Path

store = GatewayPreferenceStore(Path(os.environ.get("GATEWAY_PREF_DB", "/var/lib/cosmic/gateway/preferences.db")))
store.initialize()
print("stored pref:", store.get_cosmic_orchestrator_model())
