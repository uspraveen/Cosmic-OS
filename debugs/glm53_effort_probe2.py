import json, time, urllib.request

key = None
with open("/etc/cosmic/orchestrator.env") as f:
    for line in f:
        if line.startswith("ORCHESTRATOR_FIREWORKS_API_KEY="):
            key = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

MODEL = "accounts/fireworks/models/glm-5p3-flash"
URL = "https://api.fireworks.ai/inference/v1/chat/completions"

PROMPT = ("A website got 25 visits: 20 from one repeat visitor, 2 from a second, 2 from localhost tests, "
          "and 1 from a third. The dashboard says '4 unique visitors'. Explain in two sentences why the "
          "unique count is technically right but misleading, and state how many real human visitors there are.")

VARIANTS = [
    ("default(no param)", {}),
    ("effort=max", {"reasoning_effort": "max"}),
    ("effort=high", {"reasoning_effort": "high"}),
    ("effort=medium", {"reasoning_effort": "medium"}),
    ("effort=low", {"reasoning_effort": "low"}),
    ("effort=1024(int)", {"reasoning_effort": 1024}),
]

for label, extra in VARIANTS:
    body = {"model": MODEL, "messages": [{"role": "user", "content": PROMPT}], "max_tokens": 8000}
    body.update(extra)
    t0 = time.time()
    try:
        req = urllib.request.Request(
            URL,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            out = json.loads(resp.read())
        dt = time.time() - t0
        msg = out["choices"][0].get("message", {})
        rc = msg.get("reasoning_content") or ""
        usage = out.get("usage") or {}
        print(f"--- {label}")
        print(f"    latency={dt:.1f}s completion={usage.get('completion_tokens')} "
              f"reasoning_chars={len(rc)} "
              f"content_chars={len(msg.get('content') or '')}")
    except urllib.error.HTTPError as e:
        print(f"--- {label}: HTTP {e.code}: {e.read().decode()[:200]}")
