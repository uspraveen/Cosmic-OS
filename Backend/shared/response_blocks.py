from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .image_artifacts import is_supported_image_artifact

_CODE_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)
_ARTIFACT_MARKER_RE = re.compile(r"\[\[artifact:([^\]]+)\]\]", re.IGNORECASE)


def build_response_blocks(
    content: str | None,
    produced_artifacts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    text = str(content or "")
    artifacts = [item for item in (produced_artifacts or []) if isinstance(item, dict)]
    if not text and not artifacts:
        return []

    blocks: list[dict[str, Any]] = []
    artifact_lookup = _build_artifact_lookup(artifacts)
    consumed_artifact_ids: set[str] = set()
    markdown_index = 1
    code_index = 1
    cursor = 0

    for match in _CODE_FENCE_RE.finditer(text):
        preceding = text[cursor:match.start()]
        markdown_index = _append_markdown_with_markers(
            blocks,
            preceding,
            markdown_index=markdown_index,
            artifact_lookup=artifact_lookup,
            consumed_artifact_ids=consumed_artifact_ids,
        )

        language = str(match.group(1) or "").strip() or None
        code = str(match.group(2) or "")
        if code:
            blocks.append(
                {
                    "id": f"code_{code_index}",
                    "type": "code",
                    "language": language,
                    "code": code,
                }
            )
            code_index += 1
        cursor = match.end()

    trailing = text[cursor:]
    markdown_index = _append_markdown_with_markers(
        blocks,
        trailing,
        markdown_index=markdown_index,
        artifact_lookup=artifact_lookup,
        consumed_artifact_ids=consumed_artifact_ids,
    )

    artifact_index = 1 + sum(1 for block in blocks if str(block.get("type")) in {"image_artifact", "file_artifact"})
    for artifact in artifacts:
        artifact_id = str(artifact.get("artifact_id") or "").strip()
        if artifact_id and artifact_id in consumed_artifact_ids:
            continue
        blocks.append(_artifact_to_block(artifact, artifact_index=artifact_index))
        artifact_index += 1

    return _merge_adjacent_markdown_blocks(blocks)


def _build_artifact_lookup(artifacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for artifact in artifacts:
        for key in _artifact_lookup_keys(artifact):
            lookup.setdefault(key, artifact)
    return lookup


def _artifact_lookup_keys(artifact: dict[str, Any]) -> list[str]:
    artifact_id = str(artifact.get("artifact_id") or "").strip()
    filename = str(artifact.get("filename") or "").strip()
    logical_path = str(artifact.get("path") or "").strip()
    names = [
        artifact_id.lower(),
        filename.lower(),
        Path(filename).name.lower() if filename else "",
        Path(logical_path).name.lower() if logical_path else "",
    ]
    return [item for item in names if item]


def _append_markdown_with_markers(
    blocks: list[dict[str, Any]],
    text: str,
    *,
    markdown_index: int,
    artifact_lookup: dict[str, dict[str, Any]],
    consumed_artifact_ids: set[str],
) -> int:
    if not text:
        return markdown_index

    artifact_index = 1 + sum(1 for block in blocks if str(block.get("type")) in {"image_artifact", "file_artifact"})
    cursor = 0
    pending_parts: list[str] = []

    for match in _ARTIFACT_MARKER_RE.finditer(text):
        pending_parts.append(text[cursor:match.start()])
        marker_token = str(match.group(1) or "").strip().lower()
        artifact = artifact_lookup.get(marker_token)
        artifact_id = str(artifact.get("artifact_id") or "").strip() if isinstance(artifact, dict) else ""

        if artifact and artifact_id and artifact_id not in consumed_artifact_ids:
            pending_text = "".join(pending_parts)
            if pending_text:
                blocks.append(
                    {
                        "id": f"markdown_{markdown_index}",
                        "type": "markdown",
                        "text": pending_text,
                    }
                )
                markdown_index += 1
            blocks.append(_artifact_to_block(artifact, artifact_index=artifact_index))
            artifact_index += 1
            consumed_artifact_ids.add(artifact_id)
            pending_parts = []
        else:
            pending_parts.append(text[match.start():match.end()])
        cursor = match.end()

    pending_parts.append(text[cursor:])
    pending_text = "".join(pending_parts)
    if pending_text:
        blocks.append(
            {
                "id": f"markdown_{markdown_index}",
                "type": "markdown",
                "text": pending_text,
            }
        )
        markdown_index += 1
    return markdown_index


def _artifact_to_block(artifact: dict[str, Any], *, artifact_index: int) -> dict[str, Any]:
    artifact_id = str(artifact.get("artifact_id") or "").strip()
    filename = str(artifact.get("filename") or artifact_id or f"artifact_{artifact_index}").strip()
    block_type = "image_artifact" if is_supported_image_artifact(artifact) else "file_artifact"
    return {
        key: value
        for key, value in {
            "id": f"artifact_{artifact_index}",
            "type": block_type,
            "artifact_id": artifact_id or None,
            "filename": filename,
            "mime_type": str(artifact.get("mime_type") or artifact.get("mime") or "").strip() or None,
            "size_bytes": artifact.get("size_bytes"),
            "downloadable": bool(artifact.get("downloadable", True)),
            "kind": str(artifact.get("kind") or "").strip() or None,
        }.items()
        if value not in (None, "", [], {})
    }


def _merge_adjacent_markdown_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for block in blocks:
        if str(block.get("type")) != "markdown":
            merged.append(block)
            continue
        text = str(block.get("text") or "")
        if not merged or str(merged[-1].get("type")) != "markdown":
            merged.append(dict(block))
            continue
        previous_text = str(merged[-1].get("text") or "")
        merged[-1]["text"] = previous_text + text
    return merged
