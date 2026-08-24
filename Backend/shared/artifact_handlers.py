"""Who can claim an uploaded artifact, and what they would do with it.

This is a registry, not a dispatcher. It answers "what are the options for this
file?" and stops there; choosing between them is the orchestrator's job, because
the orchestrator is the only thing that can see the file, the conversation, and
the user's actual intent at once.

The point of keeping it declarative is that adding a consumer later is an entry
in ``ARTIFACT_HANDLERS`` rather than another branch in an ``if mime == ...``
ladder somewhere in the ingest path. A handler that claims nothing today still
documents itself here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ArtifactHandler:
    handler_id: str
    # Shown to the user when the orchestrator has to ask which one they meant.
    label: str
    # Shown to the model: what actually happens if this handler is chosen.
    summary: str
    kinds: frozenset[str]
    # The intent the orchestrator would route to, or "" for the default path
    # that already parses the artifact into conversation context.
    intent: str


ARTIFACT_HANDLERS: tuple[ArtifactHandler, ...] = (
    ArtifactHandler(
        handler_id="alpha_project",
        label="Hand it to Alpha to build with",
        summary=(
            "Stage the files in the Alpha agent's project workspace so Alpha can build, "
            "edit, or deploy them. Nothing is read into this conversation."
        ),
        kinds=frozenset({"bundle", "web_asset"}),
        intent="alpha.execute",
    ),
    ArtifactHandler(
        handler_id="read_inline",
        label="Read it and tell me about it",
        summary=(
            "Read the file's text into this conversation so it can be summarised, "
            "reviewed, or answered against. Costs context proportional to file size."
        ),
        # Deliberately NOT document/spreadsheet. Those already have a pipeline that
        # parses them into context, and they are not ambiguous -- nobody uploads a
        # PDF wondering whether it should be built into a website. Claiming them
        # here would prepend a routing note to every existing document upload,
        # changing a path that already works to describe a choice that does not
        # exist. A kind belongs in this registry only when it genuinely has more
        # than one destination.
        kinds=frozenset({"web_asset"}),
        intent="",
    ),
)


def claims_for_artifact(artifact: dict[str, Any] | None) -> list[ArtifactHandler]:
    """Handlers that can take this artifact, in registry order."""
    if not isinstance(artifact, dict):
        return []
    kind = str(artifact.get("kind") or "").strip().lower()
    if not kind:
        return []
    return [handler for handler in ARTIFACT_HANDLERS if kind in handler.kinds]


def describe_claims(artifact: dict[str, Any] | None) -> dict[str, Any]:
    """Compact, model-facing description of the routing options for one artifact.

    Deliberately returns the *options* rather than a decision. When more than one
    handler claims a file, the honest thing is for the orchestrator to ask, and
    it can only ask if it is told there was a choice.
    """
    handlers = claims_for_artifact(artifact)
    if not handlers:
        return {}
    return {
        "artifact_id": str(artifact.get("artifact_id") or "").strip(),
        "filename": str(artifact.get("filename") or "").strip(),
        "kind": str(artifact.get("kind") or "").strip(),
        "ambiguous": len(handlers) > 1,
        "options": [
            {
                "handler_id": handler.handler_id,
                "label": handler.label,
                "summary": handler.summary,
                "intent": handler.intent,
            }
            for handler in handlers
        ],
    }
