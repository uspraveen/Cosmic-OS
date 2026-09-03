"""Locating the ZCode CLI, and the environment + config it runs under.

ZCode (https://zcode.z.ai) is Z.ai's official GLM coding agent. Its CLI ships
as a single self-contained `zcode.cjs` (run with `node`); bootstrap extracts it
from the official desktop AppImage and puts a `zcode` wrapper on PATH.

Everything the CLI persists is anchored to the process home directory:
`$HOME/.zcode/cli/config.json` (model providers + the API key the login flow
writes), `$HOME/.zcode/v2/credentials.json` (OAuth tokens), and
`$HOME/.zcode/AGENTS.md` (global instructions). Every COSMIC process that runs
the CLI overrides `HOME` to the Alpha ZCode home
(`/var/lib/cosmic/alpha/homes/zcode`), so auth, sessions, and self-state stay
inside one home the Alpha workspace manager already creates — the same shape
the Cursor and OpenCode integrations use.

Auth model, verified live against the CLI:
- `zcode login` performs the Z.AI OAuth dance (remote redirect, no local
  callback server) and writes the resulting coding-plan API key into
  `cli/config.json`. That file is therefore the credential store.
- A key pasted by hand (Z.ai or BigModel) lands in the same place; per-run
  model selection then happens through `ZCODE_MODEL`/`ZCODE_BASE_URL` env
  overrides, never by editing the key.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

# Single source of truth for git credential-helper injection (shared with the
# Cursor/OpenCode CLI env builders).
from shared.cursor_cli import apply_git_credentials

ZCODE_NAMES = (
    ("zcode.exe", "zcode.cmd", "zcode")
    if os.name == "nt"
    else ("zcode",)
)

# Boxes provisioned before the wrapper lived under the Alpha home, and npm-style
# global installs.
FALLBACK_BIN_DIRS = (
    Path("/usr/local/bin"),
    Path("/usr/bin"),
    Path("/home/ubuntu/.local/bin"),
)

# The only models COSMIC offers for ZCode (user-facing setting allows exactly
# these; the CLI itself would accept anything the provider exposes).
ZCODE_MODELS: dict[str, str] = {
    "glm-5.3": "GLM-5.3",
    "glm-5.3-flash": "GLM-5.3-Flash",
}
DEFAULT_ZCODE_MODEL = "glm-5.3-flash"

# Thinking variants ZCode exposes for the GLM-5.3 family (config-level
# `reasoning.defaultVariant`; "auto" keeps the model's own default).
ZCODE_THINKING_MODES = ("auto", "low", "high", "max")
DEFAULT_ZCODE_THINKING = "auto"

ZAI_ANTHROPIC_BASE_URL = "https://api.z.ai/api/anthropic"
BIGMODEL_ANTHROPIC_BASE_URL = "https://open.bigmodel.cn/api/anthropic"

PROVIDER_ID = "zai"

_MODEL_ALIAS_KEYS = {
    "glm": "glm-5.3",
    "glm 5.3": "glm-5.3",
    "glm5.3": "glm-5.3",
    "glm-5p3": "glm-5.3",
    "glm 5p3": "glm-5.3",
    "glm53": "glm-5.3",
    "glm-53": "glm-5.3",
    "glm 5.3 flash": "glm-5.3-flash",
    "glm5.3flash": "glm-5.3-flash",
    "glm 5.3-flash": "glm-5.3-flash",
    "glm-5.3flash": "glm-5.3-flash",
    "glm-5.3-flash": "glm-5.3-flash",
    "glm53flash": "glm-5.3-flash",
    "glm-53-flash": "glm-5.3-flash",
    "flash": "glm-5.3-flash",
    "5.3 flash": "glm-5.3-flash",
    "5.3": "glm-5.3",
}


def normalize_zcode_model(value: str | None) -> str | None:
    """Map free-text model picks onto the two supported ids.

    Accepts bare ids (`glm-5.3`), qualified ids (`zai/glm-5.3`), display names
    (`GLM-5.3-Flash`), and common spoken spellings. "auto"/empty → None, which
    callers resolve against the saved preference.
    """
    normalized = str(value or "").strip()
    if not normalized or normalized.lower() == "auto":
        return None
    bare = normalized.split("/")[-1].strip()
    alias_key = " ".join(bare.lower().replace("_", " ").split())
    model_id = _MODEL_ALIAS_KEYS.get(alias_key) or _MODEL_ALIAS_KEYS.get(
        alias_key.replace(" ", "-")
    )
    if model_id:
        return model_id
    lowered = bare.lower()
    if lowered in ZCODE_MODELS:
        return lowered
    return None


def normalize_zcode_thinking(value: str | None) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in ZCODE_THINKING_MODES:
        return normalized
    # Reasoning-effort spellings from other harnesses map onto the same idea.
    if normalized in {"minimal", "medium", "xhigh"}:
        return {"minimal": "low", "medium": "high", "xhigh": "max"}[normalized]
    return DEFAULT_ZCODE_THINKING


def zcode_local_bin_dir(zcode_home: str | Path) -> Path:
    """Where the bootstrap `zcode` wrapper lives for this home."""
    return Path(zcode_home).expanduser() / ".zcode" / "cli" / "bin"


def zcode_cli_config_path(zcode_home: str | Path) -> Path:
    """The CLI's model-provider config: `$HOME/.zcode/cli/config.json`."""
    return Path(zcode_home).expanduser() / ".zcode" / "cli" / "config.json"


def zcode_global_instructions_path(zcode_home: str | Path) -> Path:
    """Global AGENTS.md the CLI reads on top of the cwd walk."""
    return Path(zcode_home).expanduser() / ".zcode" / "AGENTS.md"


def zcode_candidates(zcode_home: str | Path | None = None) -> list[Path]:
    """Every path worth probing, most authoritative first."""
    bin_dirs: list[Path] = []
    if zcode_home is not None:
        bin_dirs.append(zcode_local_bin_dir(zcode_home))
    try:
        bin_dirs.append(Path.home() / ".local" / "bin")
    except (RuntimeError, OSError):
        pass
    bin_dirs.extend(FALLBACK_BIN_DIRS)

    candidates: list[Path] = []
    seen: set[Path] = set()
    for bin_dir in bin_dirs:
        for name in ZCODE_NAMES:
            candidate = bin_dir / name
            if candidate not in seen:
                seen.add(candidate)
                candidates.append(candidate)
    return candidates


def find_zcode_binary(zcode_home: str | Path | None = None) -> str | None:
    """Absolute path to the `zcode` wrapper/binary, or None when absent.

    `PATH` still wins when it resolves, mirroring cursor/opencode resolution.
    """
    on_path = shutil.which("zcode")
    if on_path:
        return on_path
    for candidate in zcode_candidates(zcode_home):
        if candidate.exists():
            return str(candidate)
    return None


def build_zcode_provider_entry(api_key: str = "") -> dict[str, Any]:
    """The `provider.zai` block: two GLM models with all thinking variants."""
    def _model(reasoning_default: str) -> dict[str, Any]:
        return {
            "reasoning": {
                "enabled": True,
                "variants": ["low", "high", "max"],
                "defaultVariant": reasoning_default,
            },
            "limit": {"context": 1000000, "output": 128000},
            "modalities": {
                "input": ["text"],
                "output": ["text"],
            },
        }

    flash = _model("max")
    flash["modalities"]["input"] = ["text", "image", "video"]
    options: dict[str, Any] = {
        "apiKeyRequired": True,
        "baseURL": ZAI_ANTHROPIC_BASE_URL,
    }
    if api_key.strip():
        options["apiKey"] = api_key.strip()
    return {
        "kind": "anthropic",
        "name": "Z.ai - Coding Plan",
        "options": options,
        "models": {
            "glm-5.3": _model("max"),
            "glm-5.3-flash": flash,
        },
    }


def build_zcode_config_update(
    *,
    api_key: str | None = None,
    preferred_model: str | None = None,
    thinking: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Config fragment COSMIC manages, keyed exactly like `cli/config.json`.

    Only the pieces we own: the `zai` provider block and the main-model
    selection. Whatever else an existing config carries (plugins, hooks,
    storage) is merged around this, never dropped.
    """
    model_id = (
        normalize_zcode_model(preferred_model)
        or (normalize_zcode_model(DEFAULT_ZCODE_MODEL))
    )
    thinking_mode = normalize_zcode_thinking(thinking)
    provider = build_zcode_provider_entry(api_key=api_key or "")
    if base_url and base_url.strip():
        provider["options"]["baseURL"] = base_url.strip()
    update: dict[str, Any] = {
        "provider": {PROVIDER_ID: provider},
        "model": {"main": f"{PROVIDER_ID}/{model_id}"},
    }
    # "auto" means the model's own documented default (max for the GLM-5.3
    # family). Pin it explicitly so saving auto after a non-default pick
    # properly resets, and so per-run ensures are deterministic.
    effective_variant = "max" if thinking_mode == DEFAULT_ZCODE_THINKING else thinking_mode
    for entry in provider["models"].values():
        entry["reasoning"]["defaultVariant"] = effective_variant
    return update


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def read_zcode_cli_config(zcode_home: str | Path) -> dict[str, Any] | None:
    path = zcode_cli_config_path(zcode_home)
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def ensure_zcode_cli_config(
    zcode_home: str | Path,
    *,
    api_key: str | None = None,
    preferred_model: str | None = None,
    thinking: str | None = None,
    base_url: str | None = None,
) -> dict[str, Any]:
    """Idempotently merge COSMIC's model/auth settings into cli/config.json.

    Returns `{path, wrote, has_api_key, main_model}` — never the key itself.
    """
    path = zcode_cli_config_path(zcode_home)
    current: dict[str, Any] = {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            current = parsed
    except (OSError, json.JSONDecodeError):
        current = {}

    update = build_zcode_config_update(
        api_key=api_key,
        preferred_model=preferred_model,
        thinking=thinking,
        base_url=base_url,
    )
    if api_key is None:
        # A save that does not touch the key must not drop the stored one
        # (login wrote it there; hands-off means hands-off).
        existing_key = str(
            (current.get("provider") or {}).get(PROVIDER_ID, {}).get("options", {}).get("apiKey", "")
        )
        update["provider"][PROVIDER_ID]["options"]["apiKey"] = existing_key
    merged = _deep_merge(json.loads(json.dumps(current)), update)
    serialized = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        existing_text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        existing_text = None
    wrote = False
    if existing_text != serialized:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(serialized, encoding="utf-8")
        os.replace(tmp, path)
        wrote = True
    return {
        "path": str(path),
        "wrote": wrote,
        "has_api_key": bool(
            str(update["provider"][PROVIDER_ID]["options"].get("apiKey", "")).strip()
        ),
        "main_model": str(update["model"]["main"]),
    }


def read_zcode_auth_state(zcode_home: str | Path) -> dict[str, Any]:
    """Cheap credential probe for status endpoints — no subprocess needed."""
    config = read_zcode_cli_config(zcode_home)
    provider = (config or {}).get("provider", {})
    options = provider.get(PROVIDER_ID, {}).get("options", {}) if isinstance(provider, dict) else {}
    main = str((config or {}).get("model", {}).get("main", "")) if isinstance(config, dict) else ""
    configured = isinstance(provider, dict) and PROVIDER_ID in provider
    return {
        "configured": bool(configured),
        "has_api_key": bool(str(options.get("apiKey", "")).strip()),
        "base_url": str(options.get("baseURL", "")),
        "main_model": main,
    }


def zcode_cli_env(
    zcode_home: str | Path,
    *,
    model: str | None = None,
    base_url: str | None = None,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Environment for running `zcode` against the Alpha ZCode home.

    HOME (and USERPROFILE on Windows, which `os.homedir()` prefers there)
    anchor `$HOME/.zcode` — config, credentials, sessions, global AGENTS.md.
    `model` overrides the main model per run via `ZCODE_MODEL` (qualified
    `zai/<id>`), keeping the config file free of per-run churn.
    """
    env = dict(base_env if base_env is not None else os.environ)
    home = Path(zcode_home).expanduser()
    env["HOME"] = str(home)
    if os.name == "nt":
        env["USERPROFILE"] = str(home)
    path_parts = [str(zcode_local_bin_dir(home))]
    try:
        path_parts.append(str(Path.home() / ".local" / "bin"))
    except (RuntimeError, OSError):
        pass
    path_parts.append("/usr/local/bin")
    existing = env.get("PATH", "")
    if existing:
        path_parts.append(existing)
    env["PATH"] = os.pathsep.join(path_parts)

    model_id = normalize_zcode_model(model)
    if model_id:
        env["ZCODE_MODEL"] = f"{PROVIDER_ID}/{model_id}"
        env["ZCODE_BASE_URL"] = (base_url or "").strip() or ZAI_ANTHROPIC_BASE_URL
    else:
        env.pop("ZCODE_MODEL", None)
        env.pop("ZCODE_BASE_URL", None)
    # Credentials ride inside cli/config.json — never through ambient env.
    for key in ("ZCODE_API_KEY", "ZAI_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    return apply_git_credentials(env)
