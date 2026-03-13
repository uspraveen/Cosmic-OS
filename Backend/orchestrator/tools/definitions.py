"""Anthropic-native tool definitions for the COSMIC orchestrator.

Each tool definition follows the Anthropic Messages API tool schema exactly.
The orchestrator passes these to Claude Opus via the `tools` parameter.
"""
from __future__ import annotations

from typing import Any


ORCHESTRATOR_TOOLS: list[dict[str, Any]] = [
    # ── Web Search ──────────────────────────────────────────────
    {
        "name": "web_search",
        "description": (
            "Search the web for current information using a natural-language query. "
            "Returns a concise answer with citations. Use this for real-time facts, "
            "news, weather, prices, documentation lookups, or any question that benefits "
            "from up-to-date web data."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The search query. Be specific and descriptive for best results.",
                },
            },
            "required": ["query"],
        },
    },
    # ── Memory Search ───────────────────────────────────────────
    {
        "name": "memory_search",
        "description": (
            "Search the user's long-term memory for previously stored facts, preferences, "
            "relationships, past conversations, and personal context. Use when the user "
            "references something from the past, asks 'do you remember', or when prior "
            "context would improve your answer. Returns relevant memory entries ranked by "
            "relevance."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural-language search query describing what to look for in memory.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of memory entries to return. Default 5.",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    },
    # ── Memory Write ────────────────────────────────────────────
    {
        "name": "memory_write",
        "description": (
            "Write a new fact, preference, or important detail to the user's long-term memory. "
            "Use when the user shares personal information worth remembering across sessions: "
            "preferences, relationships, important dates, project details, goals, corrections "
            "to prior knowledge. Do NOT write trivial conversational content. Be selective — "
            "only persist genuinely useful context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": (
                        "The memory content to store. Write as a clear, standalone factual statement. "
                        "Example: 'User prefers dark mode in all applications.'"
                    ),
                },
                "kind": {
                    "type": "string",
                    "description": "Category of memory.",
                    "enum": [
                        "preference",
                        "fact",
                        "relationship",
                        "goal",
                        "event",
                        "note",
                    ],
                },
                "title": {
                    "type": "string",
                    "description": "Short title summarizing this memory (2-8 words).",
                },
            },
            "required": ["content", "kind", "title"],
        },
    },
    # ── Create Reminder / Schedule ──────────────────────────────
    {
        "name": "create_reminder",
        "description": (
            "Create a scheduled reminder or recurring cron job. The reminder fires as a "
            "TaskEnvelope to the orchestrator at the specified time, appearing as a proactive "
            "message in the user's conversation. Use for: 'remind me at 5pm', 'every Monday "
            "morning check my calendar', 'in 2 hours ask me about X'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "label": {
                    "type": "string",
                    "description": "Human-readable label for the reminder. Shown in schedule listings.",
                },
                "cron_expression": {
                    "type": "string",
                    "description": (
                        "Standard 5-field cron expression (minute hour day-of-month month day-of-week). "
                        "Examples: '0 17 * * *' = daily at 5pm, '30 9 * * 1' = Monday 9:30am, "
                        "'0 */2 * * *' = every 2 hours. Use the user's local timezone."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "The message or instruction that will be sent to the orchestrator when "
                        "the reminder fires. Example: 'Remind the user about their dentist appointment tomorrow.'"
                    ),
                },
                "one_shot": {
                    "type": "boolean",
                    "description": "If true, the reminder fires once and is then deleted. Default true for reminders, false for recurring tasks.",
                    "default": True,
                },
            },
            "required": ["label", "cron_expression", "prompt"],
        },
    },
    # ── List Reminders ──────────────────────────────────────────
    {
        "name": "list_reminders",
        "description": (
            "List all active scheduled reminders and cron jobs. Use when the user asks "
            "'what reminders do I have', 'show my schedule', or when you need to check "
            "for existing schedules before creating a new one."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    # ── Delete Reminder ─────────────────────────────────────────
    {
        "name": "delete_reminder",
        "description": (
            "Delete a scheduled reminder or cron job by its ID. Use when the user says "
            "'cancel that reminder', 'stop the daily check', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "cron_id": {
                    "type": "string",
                    "description": "The ID of the cron/reminder to delete. Get this from list_reminders.",
                },
            },
            "required": ["cron_id"],
        },
    },
]


def get_tool_definitions() -> list[dict[str, Any]]:
    """Return a copy of the orchestrator tool definitions."""
    return [dict(tool) for tool in ORCHESTRATOR_TOOLS]
