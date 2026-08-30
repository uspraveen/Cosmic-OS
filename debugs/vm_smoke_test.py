"""One-off VM smoke test: submit a trivial signed task straight to the
orchestrator, measure end-to-end latency, and confirm the turn completes on
the new fast default reasoning path. Run with sudo on the VM:
    sudo /home/ubuntu/Cosmic-OS/Backend/.venv/bin/python /tmp/vm_smoke_test.py
"""
import json
import sys
import time

sys.path.insert(0, "/home/ubuntu/Cosmic-OS/Backend")

import httpx
from shared import TaskEnvelope, sign_task_envelope, utcnow

token = None
secret = None
with open("/etc/cosmic/gateway.env") as f:
    for line in f:
        line = line.strip()
        if line.startswith("GATEWAY_INTERNAL_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
        if line.startswith("GATEWAY_SIGNING_SECRET="):
            secret = line.split("=", 1)[1].strip().strip('"').strip("'")
if not token or not secret:
    raise SystemExit("missing GATEWAY_INTERNAL_TOKEN / GATEWAY_SIGNING_SECRET in /etc/cosmic/gateway.env")

now = int(time.time())
task = TaskEnvelope(
    task_id=f"tsk_smoke_{now}",
    task_list_id="sess_20260830",
    session_id="sess_20260830",
    sender="cosmic/gateway:1.0.0",
    recipient="cosmic/orchestrator:1.0.0",
    intent="orchestrator.process",
    input={
        "query": "Reply with exactly: OK",
        "request_id": f"req_smoke_{now}",
        "cosmic_orchestrator_model": {
            "provider": "fireworks_glm",
            "model": "accounts/fireworks/models/glm-5p3-flash",
        },
    },
    idempotency_key=f"idem_smoke_{now}",
    priority="high",
    signature="",
    created_at=utcnow(),
    source="user",
    source_id="desktop",
    channel="desktop:desk_smoketest",
)
task = task.model_copy(update={"signature": sign_task_envelope(task, secret)})

t0 = time.time()
with httpx.Client(timeout=180) as client:
    with client.stream(
        "POST",
        "http://127.0.0.1:8743/internal/process/stream",
        headers={"X-Internal-Token": token, "Content-Type": "application/json"},
        json=task.model_dump(mode="json"),
    ) as resp:
        print("HTTP", resp.status_code)
        for line in resp.iter_lines():
            line = line.strip()
            if not line:
                continue
            event = json.loads(line)
            etype = event.get("type")
            if etype == "task.created":
                print("task.created", "%.1fs" % (time.time() - t0))
            elif etype in ("response.complete", "task.failed", "task.cancelled"):
                print("FINAL:", etype, "at %.1fs" % (time.time() - t0))
                print("metrics:", json.dumps(event.get("metrics") or {}, default=str)[:600])
                print("content:", str(event.get("content") or "")[:120])
print("TOTAL: %.1fs" % (time.time() - t0))
