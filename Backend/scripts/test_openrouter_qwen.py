"""
Smoke test: OpenRouter + OpenAI SDK with qwen/qwen3.6-plus:free.

Set your key (do not commit it):
  PowerShell:  $env:OPENROUTER_API_KEY = "sk-or-v1-..."
  bash:        export OPENROUTER_API_KEY="sk-or-v1-..."

Run from repo root or Backend:
  python Backend/scripts/test_openrouter_qwen.py
"""

from __future__ import annotations

import os
import sys

from openai import OpenAI

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
MODEL = "qwen/qwen3.6-plus:free"


def _reasoning_text_from_details(details) -> str:
    if not details:
        return ""
    out: list[str] = []
    for item in details:
        if isinstance(item, dict):
            chunk = item.get("text") or item.get("summary")
        else:
            chunk = getattr(item, "text", None) or getattr(item, "summary", None)
        if chunk:
            out.append(str(chunk))
    return "".join(out)


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        print("Missing OPENROUTER_API_KEY in environment.", file=sys.stderr)
        return 1

    client = OpenAI(base_url=OPENROUTER_BASE, api_key=key)

    # Your setting (kept). Alibaba for qwen3.6 returns 400 for temperature > 1.0 on OpenRouter.
    user_temperature = 2.0
    api_temperature = min(float(user_temperature), 1.0)

    stream = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "user", "content": 'Reply with a lovely message'},
        ],
        
        temperature=api_temperature,
        stream=True,
        extra_body={
            "reasoning": {
                "enabled": True,
                "max_tokens": 256,
            },
        },
    )

    print("Model:", MODEL)

    saw_thinking = False
    saw_response = False

    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue

        rd = getattr(delta, "reasoning_details", None)
        reasoning = getattr(delta, "reasoning", None)
        if reasoning not in (None, ""):
            thinking_chunk = str(reasoning)
        else:
            thinking_chunk = _reasoning_text_from_details(rd)
        if thinking_chunk:
            if not saw_thinking:
                print("\n--- Thinking ---", flush=True)
                saw_thinking = True
            print(thinking_chunk, end="", flush=True)

        content = getattr(delta, "content", None)
        if content:
            if not saw_response:
                print("\n\n--- Response ---", flush=True)
                saw_response = True
            print(content, end="", flush=True)

    print(flush=True)
    if not saw_thinking:
        print(
            "\n--- Thinking ---\n(no reasoning chunks in this stream; model may not expose them)",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
