import json
import urllib.request

body = json.dumps(
    {
        "query": "YT specialist YouTube video Parag",
        "max_results": 5,
        "kinds": ["session_summary", "task_summary", "agent_note", "user_data", "transcript"],
    }
).encode()

req = urllib.request.Request(
    "http://127.0.0.1:8090/v1/query/passive",
    data=body,
    headers={
        "Content-Type": "application/json",
        "X-Internal-Token": open("/tmp/mem_token.txt").read().strip(),
    },
)
with urllib.request.urlopen(req, timeout=20) as resp:
    payload = json.load(resp)

items = payload.get("items", [])
print(f"[{len(items)} items]")
for item in items:
    meta = item.get("metadata") or {}
    print(
        "-",
        item.get("kind"),
        "|",
        (item.get("title") or "")[:80],
        "|",
        (item.get("content") or "")[:120].replace("\n", " "),
    )
