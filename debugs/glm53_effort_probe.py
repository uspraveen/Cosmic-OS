import json, os, time, urllib.request

key = None
for path in ("/etc/cosmic/orchestrator.env", "/etc/cosmic/gateway.env"):
    try:
        with open(path) as f:
            for line in f:
                if line.startswith("ORCHESTRATOR_FIREWORKS_API_KEY=") or line.startswith("FIREWORKS_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
        if key:
            break
    except (FileNotFoundError, PermissionError):
        pass

if not key:
    raise SystemExit("no key found in env files")

MODEL = "accounts/fireworks/models/glm-5p3-flash"
URL = "https://api.fireworks.ai/inference/v1/chat/completions"

PROMPT = "A farmer has 17 sheep. All but 9 run away. How many are left? Answer in one short sentence."

VARIANTS = [
    ("default(no param)", {}),
    ("effort=max", {"reasoning_effort": "max"}),
    ("effort=high", {"reasoning_effort": "high"}),
    ("effort=medium", {"reasoning_effort": "medium"}),
    ("effort=low", {"reasoning_effort": "low"}),
    ("effort=none", {"reasoning_effort": "none"}),
    ("effort=512(int)", {"reasoning_effort": 512}),
]

for label, extra in VARIANTS:
    body = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "max_tokens": 3000,
    }
    body.update(extra)
    t0 = time.time()
    try:
        req = urllib.request.Request(
            URL,
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            out = json.loads(resp.read())
        dt = time.time() - t0
        ch = out["choices"][0]
        msg = ch.get("message", {})
        rc = msg.get("reasoning_content") or ""
        usage = out.get("usage") or {}
        det = usage.get("completion_tokens_details") or {}
        print(f"--- {label}")
        print(f"    latency={dt:.1f}s finish={ch.get('finish_reason')} "
              f"prompt={usage.get('prompt_tokens')} completion={usage.get('completion_tokens')} "
              f"reasoning_tokens={det.get('reasoning_tokens')}")
        print(f"    reasoning_chars={len(rc)} content={ (msg.get('content') or '')[:80]!r}")
    except urllib.error.HTTPError as e:
        print(f"--- {label}: HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        print(f"--- {label}: ERR {e}")
