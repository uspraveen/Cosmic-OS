from __future__ import annotations

from typing import Any

_TRUNCATION_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("markdown_truncated", "markdown_full_artifact", "page.md"),
    ("html_truncated", "html_full_artifact", "page.html"),
    ("raw_html_truncated", "raw_html_full_artifact", "page.raw.html"),
)


def enrich_firecrawl_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    """Add explicit truncation guidance so the model knows how to load full scrape bodies."""
    if not isinstance(result, dict) or result.get("error"):
        return result

    data = result.get("data")
    if not isinstance(data, dict):
        return result

    truncated: list[dict[str, str]] = []
    for flag_key, artifact_key, default_filename in _TRUNCATION_FIELDS:
        if data.get(flag_key) is not True:
            continue
        artifact_name = str(data.get(artifact_key) or default_filename).strip() or default_filename
        truncated.append(
            {
                "field": flag_key.removesuffix("_truncated"),
                "full_artifact_filename": artifact_name,
            }
        )

    if not truncated:
        return result

    artifacts = result.get("artifacts")
    artifact_refs: list[dict[str, Any]] = [
        dict(item) for item in artifacts if isinstance(item, dict)
    ] if isinstance(artifacts, list) else []

    guidance_steps: list[str] = [
        "Inline scrape excerpts are truncated by design. Do not edit or reason from excerpts alone when full page text is required.",
    ]
    for item in truncated:
        filename = item["full_artifact_filename"]
        matched = next(
            (
                ref
                for ref in artifact_refs
                if str(ref.get("filename") or "").strip() == filename
                or str(ref.get("path") or "").endswith(f"/{filename}")
            ),
            None,
        )
        if isinstance(matched, dict):
            artifact_id = str(matched.get("artifact_id") or "").strip()
            path = str(matched.get("path") or "").strip()
            if artifact_id and path:
                guidance_steps.append(
                    f"Load the full {item['field']} body with artifact_read(artifact_id={artifact_id!r}, path={path!r})."
                )
                continue
        guidance_steps.append(
            f"Load the full {item['field']} body with artifact_read(path=...) using the matching entry from this tool result's artifacts list ({filename})."
        )

    guidance_steps.append(
        "Alternatively, call firecrawl_recall_session for this session_id to list prior Firecrawl runs and their artifact_refs."
    )

    enriched = dict(result)
    enriched["truncation_detected"] = True
    enriched["truncated_fields"] = truncated
    enriched["full_content_guidance"] = " ".join(guidance_steps)
    return enriched
