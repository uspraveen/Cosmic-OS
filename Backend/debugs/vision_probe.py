"""One-off probe: which deployed models accept image inputs?"""
import base64
import os
import re
from pathlib import Path


def parse(p):
    env = {}
    try:
        for line in Path(p).read_text().splitlines():
            m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
            if m:
                val = m.group(2).strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
                    val = val[1:-1]
                env[m.group(1)] = val
    except Exception:
        pass
    return env


env = {**parse("/etc/cosmic/orchestrator.env"), **os.environ}
key = env.get("ORCHESTRATOR_FIREWORKS_API_KEY")
base = (env.get("ORCHESTRATOR_FIREWORKS_BASE_URL") or "https://api.fireworks.ai/inference/v1").rstrip("/")

# 1x1 red PNG
png = base64.b64encode(bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
    "53de0000000c4944415408d763f8cfc000000301010018dd8db00000000049454e44ae426082"
)).decode()

candidates = [
    "accounts/fireworks/models/glm-5p3-flash",
    "accounts/fireworks/models/deepseek-v4-flash-vision-exp",
    "accounts/fireworks/models/qwen3p8-max",
    "accounts/fireworks/models/qwen3p7-plus",
    "accounts/fireworks/models/qwen3p8-2p4t-a95b",
]
payload_template = {
    "messages": [{
        "role": "user",
        "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}},
            {"type": "text", "text": "What color is this image? One word."},
        ],
    }],
    "max_tokens": 200,
}

import httpx  # noqa: E402

with httpx.Client(timeout=60) as c:
    for model in candidates:
        payload = {**payload_template, "model": model}
        try:
            r = c.post(f"{base}/chat/completions", json=payload, headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 200:
                msg = r.json()["choices"][0]["message"].get("content", "")
                print(f"{model}: 200 → {msg[:40]!r}")
            else:
                print(f"{model}: {r.status_code} {r.text[:80]}")
        except Exception as exc:
            print(f"{model}: ERROR {exc}")
