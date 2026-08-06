"""The Google auth-health probe must ask for the agent's baseline scopes.

It used to demand the union of every intent's scopes. That meant adding one
extra scope to a single advanced intent (drive.readonly, needed only to
discover documents the user created outside Cosmic) made the entire Docs
agent report reauth_required for every connected account until the user
re-consented - 582 reauth events in one 24h window - even though reading,
creating and editing documents still worked perfectly under the scopes they
already had.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from shared.agent_runtime import AgentRuntime

DOCS = "https://www.googleapis.com/auth/documents"
DRIVE_FILE = "https://www.googleapis.com/auth/drive.file"
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"


def _agent(agent_id: str, auth_requirements: dict) -> AgentRuntime:
    runtime = AgentRuntime.__new__(AgentRuntime)
    runtime.agent_id = agent_id
    runtime.agent_card = {"auth_requirements": auth_requirements}
    return runtime


def test_probe_uses_the_baseline_every_intent_shares() -> None:
    config = _agent(
        "cosmic/google-docs-agent:1.0.0",
        {
            "docs.resolve_resource": {
                "provider": "google",
                "scopes": [DOCS, DRIVE_FILE, DRIVE_READONLY],
            },
            "docs.read": {"provider": "google", "scopes": [DOCS, DRIVE_FILE]},
            "docs.edit": {"provider": "google", "scopes": [DOCS, DRIVE_FILE]},
        },
    )._google_provider_health_probe_config()

    assert config is not None
    assert config["required_scopes"] == [DOCS, DRIVE_FILE]
    assert DRIVE_READONLY not in config["required_scopes"], (
        "a scope only one advanced intent needs must not make the whole agent "
        "report reauth_required"
    )


def test_single_intent_agents_are_unaffected() -> None:
    """Every other Google agent has one uniform scope set, so the change must
    be a no-op for them."""
    gmail = "https://www.googleapis.com/auth/gmail.modify"
    config = _agent(
        "cosmic/gmail-agent:1.0.0",
        {"gmail.search": {"provider": "google", "scopes": [gmail]}},
    )._google_provider_health_probe_config()
    assert config["required_scopes"] == [gmail]


def test_uniform_multi_intent_agents_are_unaffected() -> None:
    sheets = "https://www.googleapis.com/auth/spreadsheets"
    config = _agent(
        "cosmic/google-sheets-agent:1.0.0",
        {
            "sheets.read": {"provider": "google", "scopes": [sheets, DRIVE_FILE]},
            "sheets.edit": {"provider": "google", "scopes": [sheets, DRIVE_FILE]},
        },
    )._google_provider_health_probe_config()
    assert config["required_scopes"] == [DRIVE_FILE, sheets]


def test_fully_disjoint_intents_fall_back_to_the_union() -> None:
    """An empty baseline would mean probing nothing at all, which would make
    the probe silently useless rather than merely strict."""
    config = _agent(
        "cosmic/google-docs-agent:1.0.0",
        {
            "a": {"provider": "google", "scopes": [DOCS]},
            "b": {"provider": "google", "scopes": [DRIVE_FILE]},
        },
    )._google_provider_health_probe_config()
    assert config["required_scopes"] == [DOCS, DRIVE_FILE]


def test_non_google_requirements_are_ignored() -> None:
    config = _agent(
        "cosmic/google-docs-agent:1.0.0",
        {
            "docs.read": {"provider": "google", "scopes": [DOCS, DRIVE_FILE]},
            "notion.read": {"provider": "notion", "scopes": ["notion.read"]},
        },
    )._google_provider_health_probe_config()
    assert config["required_scopes"] == [DOCS, DRIVE_FILE]


def test_no_google_requirements_disables_the_probe() -> None:
    assert (
        _agent(
            "cosmic/google-docs-agent:1.0.0",
            {"notion.read": {"provider": "notion", "scopes": ["notion.read"]}},
        )._google_provider_health_probe_config()
        is None
    )
