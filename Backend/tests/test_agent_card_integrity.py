"""Agent cards must survive YAML parsing as the text they look like.

A usage hint is written as a bare YAML scalar, so a `: ` anywhere inside one
turns the whole hint into a single-key mapping instead of a string. Nothing
raises: the card loads, registers, and reaches the orchestrator's specialist
catalog with a hint that is now a dict. The guidance is silently mangled at
exactly the layer whose entire job is telling the orchestrator which specialist
to pick.

This bit while adding a hint containing "...with other people: replying to...".
It is invisible on inspection and cheap to check, so check every card.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from shared.content_cards import INTENT_PRESENTATION, SPECIALIST_VIEW_REGISTRY

BACKEND_ROOT = Path(__file__).resolve().parents[1]
CARD_PATHS = sorted(BACKEND_ROOT.glob("agents/*/agent_card.yaml"))


def test_agent_cards_are_discoverable() -> None:
    assert CARD_PATHS, "no agent cards found -- the glob is wrong, not the repo"


@pytest.mark.parametrize("card_path", CARD_PATHS, ids=lambda p: p.parent.name)
def test_every_card_field_parses_as_the_text_it_looks_like(card_path: Path) -> None:
    card = yaml.safe_load(card_path.read_text(encoding="utf-8"))
    assert isinstance(card, dict), f"{card_path} did not parse as a mapping"

    agent_id = card.get("agent_id")
    assert isinstance(agent_id, str) and agent_id.strip(), f"{card_path} has no agent_id"

    intents = card.get("intents")
    assert isinstance(intents, list) and intents, f"{card_path} declares no intents"

    for intent in intents:
        assert isinstance(intent, dict), f"{card_path} has a non-mapping intent entry"
        name = intent.get("name")
        assert isinstance(name, str) and name.strip(), f"{card_path} has an unnamed intent"

        description = intent.get("description")
        assert isinstance(description, str), (
            f"{card_path} [{name}] description parsed as {type(description).__name__}, "
            "not str -- a ': ' in the text turned it into a mapping"
        )

        for index, hint in enumerate(intent.get("usage_hints") or []):
            assert isinstance(hint, str), (
                f"{card_path} [{name}] usage_hint {index} parsed as "
                f"{type(hint).__name__}, not str. A ': ' inside a bare YAML scalar "
                "makes it a mapping. Rephrase with a dash or period, or quote the hint."
            )

        presentation = intent.get("presentation")
        if presentation is None:
            continue
        assert isinstance(presentation, dict), (
            f"{card_path} [{name}] presentation parsed as {type(presentation).__name__}, not dict"
        )
        view = presentation.get("view")
        data_path = presentation.get("data_path")
        assert isinstance(view, str) and view.strip(), f"{card_path} [{name}] presentation.view is missing"
        assert view in SPECIALIST_VIEW_REGISTRY, (
            f"{card_path} [{name}] presentation.view {view!r} is not a Cosmic-owned renderer"
        )
        assert isinstance(data_path, str) and data_path.strip(), (
            f"{card_path} [{name}] presentation.data_path is missing"
        )
        registered = INTENT_PRESENTATION.get(name)
        if registered:
            assert registered["view"] == view
            assert registered["data_path"] == data_path


def test_x_search_declares_the_registered_content_projection() -> None:
    spec = INTENT_PRESENTATION["x.search"]
    assert spec["view"] in SPECIALIST_VIEW_REGISTRY
    assert spec["data_path"] == "notable_posts"

