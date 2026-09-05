"""One-off probe 2: does response_format json_schema trigger the reasoning_effort=none rejection?"""
import os

import httpx

import llm_probe  # merges the env files into os.environ at import

base = os.environ["MODEL_BASE_URL"].rstrip("/")
key = os.environ["MODEL_API_KEY"]
model = "accounts/fireworks/models/glm-5p3"
schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
variants = {
    "json_schema+temp0": {"model": model, "messages": [{"role": "user", "content": "Reply with JSON ok=true."}], "stream": False, "temperature": 0.0, "max_tokens": 500, "response_format": {"type": "json_schema", "json_schema": {"name": "structured_output", "schema": schema}}},
    "json_schema+temp0.2": {"model": model, "messages": [{"role": "user", "content": "Reply with JSON ok=true."}], "stream": False, "temperature": 0.2, "max_tokens": 500, "response_format": {"type": "json_schema", "json_schema": {"name": "structured_output", "schema": schema}}},
    "json_object": {"model": model, "messages": [{"role": "user", "content": "Reply with JSON ok=true."}], "stream": False, "temperature": 0.0, "max_tokens": 500, "response_format": {"type": "json_object"}},
}
with httpx.Client(timeout=60) as c:
    for name, payload in variants.items():
        r = c.post(f"{base}/chat/completions", json=payload, headers={"Authorization": f"Bearer {key}"})
        body = r.json()
        if r.status_code == 200:
            msg = body.get("choices", [{}])[0].get("message", {}).get("content")
        else:
            msg = body.get("message", r.text[:140])
        print(name, r.status_code, repr((msg or "")[:60]))
