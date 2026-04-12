from __future__ import annotations

import json
import logging
import textwrap
from typing import Any, Callable

from agent_tools import ToolContext, ToolDefinition, ToolExecutionError

logger = logging.getLogger(__name__)


def _compact_value(value: Any, *, max_depth: int = 4, max_items: int = 8, max_chars: int = 1400) -> Any:
    if max_depth <= 0:
        text = str(value or "").strip()
        return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for idx, (key, item) in enumerate(value.items()):
            if idx >= max_items:
                out["__truncated__"] = f"{len(value) - max_items} more fields omitted"
                break
            out[str(key)] = _compact_value(item, max_depth=max_depth - 1, max_items=max_items, max_chars=max_chars)
        return out
    if isinstance(value, list):
        items = [
            _compact_value(item, max_depth=max_depth - 1, max_items=max_items, max_chars=max_chars)
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            items.append(f"... {len(value) - max_items} more items omitted")
        return items
    if isinstance(value, str):
        return value if len(value) <= max_chars else value[: max_chars - 1].rstrip() + "…"
    return value


def _tool_protocol_block(
    tools: list[ToolDefinition],
    *,
    final_hint: str,
    allow_asset_ids: bool,
    max_tool_rounds: int,
) -> str:
    lines = [
        "OPTIONAL TOOL USE",
        "You may use tools before returning the final JSON if research, bespoke visuals, or generated artifacts would materially improve the result.",
        'If you want tool use, return ONLY JSON in this shape: {"tool_calls":[{"tool":"name","arguments":{...}}]}',
        "Do not mix tool_calls with the final answer in the same response.",
        f"You may use at most {max_tool_rounds} tool rounds and at most 3 tool calls in a single response.",
        f"When you are done, return ONLY the final JSON for this task: {final_hint}",
    ]
    if allow_asset_ids:
        lines.append('If a tool result returns generated assets, reference them in the final JSON with "asset_id".')
    lines.append("Available tools:")
    for tool in tools:
        lines.append(
            f"- {tool.name}: {tool.description}\n"
            f"  Input schema: {json.dumps(tool.input_schema, ensure_ascii=False)}"
        )
    return "\n".join(lines)


def _normalize_tool_calls(payload: dict[str, Any]) -> list[dict[str, Any]]:
    calls = payload.get("tool_calls")
    if not isinstance(calls, list):
        return []
    normalized: list[dict[str, Any]] = []
    for raw in calls:
        if not isinstance(raw, dict):
            continue
        tool_name = str(raw.get("tool") or raw.get("name") or "").strip()
        if not tool_name:
            continue
        args = raw.get("arguments")
        if not isinstance(args, dict):
            args = {}
        normalized.append({"tool": tool_name, "arguments": args})
    return normalized


def run_json_stage_with_tools(
    messages: list[dict[str, Any]],
    *,
    call_llm: Callable[[list[dict[str, Any]], float], str],
    parse_json: Callable[[str], dict[str, Any]],
    is_final_result: Callable[[dict[str, Any]], bool],
    tools: list[ToolDefinition],
    tool_context: ToolContext,
    final_hint: str,
    temperature: float,
    max_tool_rounds: int = 3,
    allow_asset_ids: bool = False,
) -> tuple[dict[str, Any], dict[str, str]]:
    if not tools:
        raw = call_llm(messages, temperature)
        return parse_json(raw), {}

    tool_prompt = {
        "role": "system",
        "content": _tool_protocol_block(
            tools,
            final_hint=final_hint,
            allow_asset_ids=allow_asset_ids,
            max_tool_rounds=max_tool_rounds,
        ),
    }
    conversation = list(messages)
    if conversation and conversation[0].get("role") == "system":
        conversation = [conversation[0], tool_prompt, *conversation[1:]]
    else:
        conversation = [tool_prompt, *conversation]

    tool_map = {tool.name: tool for tool in tools}
    asset_paths: dict[str, str] = {}

    for round_idx in range(max_tool_rounds + 1):
        raw = call_llm(conversation, temperature)
        payload = parse_json(raw)
        if is_final_result(payload):
            return payload, asset_paths

        tool_calls = _normalize_tool_calls(payload)
        if not tool_calls:
            raise ValueError("Model returned neither a final JSON result nor valid tool_calls.")
        if round_idx >= max_tool_rounds:
            raise ValueError("Model requested additional tool calls after the tool-round limit was reached.")

        results_for_model: list[dict[str, Any]] = []
        for call in tool_calls[:3]:
            tool_name = call["tool"]
            arguments = call["arguments"]
            tool = tool_map.get(tool_name)
            if tool is None:
                results_for_model.append({
                    "tool": tool_name,
                    "ok": False,
                    "error": f"Unknown tool: {tool_name}",
                })
                continue
            try:
                result = tool.executor(arguments, tool_context)
                generated_assets = result.get("generated_assets") if isinstance(result, dict) else None
                if isinstance(generated_assets, list):
                    for item in generated_assets:
                        if not isinstance(item, dict):
                            continue
                        asset_id = str(item.get("asset_id") or "").strip()
                        path = str(item.get("path") or "").strip()
                        if asset_id and path:
                            asset_paths[asset_id] = path
                result_for_model = _compact_value(result)
                if isinstance(result_for_model, dict):
                    result_for_model.pop("generated_assets", None)
                results_for_model.append({
                    "tool": tool_name,
                    "ok": True,
                    "arguments": _compact_value(arguments),
                    "result": result_for_model,
                })
            except ToolExecutionError as exc:
                logger.warning("tool %s failed: %s", tool_name, exc)
                results_for_model.append({
                    "tool": tool_name,
                    "ok": False,
                    "arguments": _compact_value(arguments),
                    "error": str(exc),
                })
            except Exception as exc:
                logger.exception("tool %s crashed", tool_name)
                results_for_model.append({
                    "tool": tool_name,
                    "ok": False,
                    "arguments": _compact_value(arguments),
                    "error": f"Unhandled tool failure: {exc}",
                })

        conversation.append({"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)})
        conversation.append({
            "role": "user",
            "content": textwrap.dedent(
                f"""\
                TOOL RESULTS
                Use these results to continue. If you have enough information now, return the final JSON only.

                {json.dumps({"tool_results": results_for_model}, ensure_ascii=False, indent=2)}
                """
            ).strip(),
        })

    raise ValueError("Tool harness exited without a final result.")
