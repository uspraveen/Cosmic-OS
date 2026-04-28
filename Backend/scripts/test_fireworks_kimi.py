"""
Smoke test: Fireworks AI + OpenAI SDK (OpenAI-compatible HTTP API).

Uses vision (image URL + text) like the Fireworks REST example, but via the
official `openai` client pointing at Fireworks' base URL.

Thinking / reasoning: Fireworks streams internal reasoning in
`delta.reasoning_content` (see Fireworks "Reasoning" guide). That is only
filled for reasoning models such as `accounts/fireworks/models/kimi-k2-thinking`.

Important: On Fireworks, **Kimi K2 Thinking does not support image input** (per
model page). `kimi-k2p5` supports vision but does not expose `reasoning_content`.
So you choose either **vision (k2p5)** or **thinking stream (k2-thinking,
text-only)** — not both on these IDs.

Optional: set model override
  $env:FIREWORKS_KIMI_MODEL = "accounts/fireworks/models/kimi-k2-thinking"

Set your key (do not commit it):
  PowerShell:  $env:FIREWORKS_API_KEY = "fw_..."
  bash:        export FIREWORKS_API_KEY="fw_..."

Run:
  python Backend/scripts/test_fireworks_kimi.py
"""

from __future__ import annotations

import os
import sys

from openai import OpenAI

FIREWORKS_BASE = "https://api.fireworks.ai/inference/v1"
DEFAULT_MODEL = "accounts/fireworks/models/kimi-k2p5"
THINKING_MODEL = "accounts/fireworks/models/kimi-k2-thinking"

# Example image from the Fireworks sample (public URL).
SAMPLE_IMAGE_URL = (
    "https://images.unsplash.com/photo-1582538885592-e70a5d7ab3d3"
    "?ixlib=rb-4.0.3&ixid=M3wxMjA3fDB8MHxwaG90by1wYWdlfHx8fGVufDB8fHx8fA%3D%3D"
    "&auto=format&fit=crop&w=1770&q=80"
)


def main() -> int:
    if sys.platform == "win32" and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    key = os.getenv("FIREWORKS_API_KEY", "").strip()
    if not key:
        print("Missing FIREWORKS_API_KEY in environment.", file=sys.stderr)
        return 1

    client = OpenAI(base_url=FIREWORKS_BASE, api_key=key)

    model = os.getenv("FIREWORKS_KIMI_MODEL", DEFAULT_MODEL).strip()
    use_reasoning_model = "thinking" in model.lower()

    if use_reasoning_model:
        # kimi-k2-thinking: reasoning_content streamed, but no vision on Fireworks.
        messages = [
            {
                "role": "user",
                "content": (
                    "What's bigger, 9.9 or 9.11? Give a short final answer after reasoning."
                ),
            }
        ]
        extra_body: dict = {"top_k": 40, "reasoning_effort": "medium"}
    else:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Can you describe this image. What's something not everyone would see?"
                        ),
                    },
                    {
                        "type": "image_url",
                        "image_url": {"url": SAMPLE_IMAGE_URL},
                    },
                ],
            }
        ]
        extra_body = {"top_k": 40}

    kwargs = {
        "model": model,
        "messages": messages,
        "max_tokens": 32768,
        "temperature": 0.6,
        "top_p": 1.0,
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "stream": True,
        "extra_body": extra_body,
    }

    print("Model:", model)
    if use_reasoning_model:
        print("(reasoning model — text-only prompt; set FIREWORKS_KIMI_MODEL unset for vision k2p5)")
    else:
        print(f"(vision — for streamed thinking, set FIREWORKS_KIMI_MODEL={THINKING_MODEL!r})")

    saw_thinking = False
    saw_response = False

    stream = client.chat.completions.create(**kwargs)

    for chunk in stream:
        if not chunk.choices:
            continue
        delta = chunk.choices[0].delta
        if delta is None:
            continue

        rc = getattr(delta, "reasoning_content", None)
        if rc:
            if not saw_thinking:
                print("\n--- Thinking (reasoning_content) ---", flush=True)
                saw_thinking = True
            print(rc, end="", flush=True)

        content = getattr(delta, "content", None)
        if content:
            if not saw_response:
                print("\n\n--- Response ---", flush=True)
                saw_response = True
            print(content, end="", flush=True)

    print(flush=True)
    if not saw_thinking:
        hint = (
            "This run used a non-reasoning model (e.g. k2p5): Fireworks does not send reasoning_content."
            if not use_reasoning_model
            else "No reasoning_content in stream — try reasoning_effort high/low or check API response."
        )
        print(f"\n--- Thinking ---\n({hint})", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
