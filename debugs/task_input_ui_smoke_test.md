# Task Input UI Smoke Test

This is the exact reproducible process for sending a real `task.input_required` event through the live Gateway so the desktop Task UI can be tested end to end.

This test is intentionally lightweight:
- it does **not** require creating a real long-running Opus task first
- it publishes a single pending task-input request into the same Redis stream the orchestrator uses
- the Gateway then consumes it and delivers it to the active desktop channel exactly like a real suspended task

## What this tests

- Gateway consumption of `user_input:requests`
- `task.input_required` delivery to the active desktop WebSocket channel
- desktop floating interrupt
- desktop `Task Inbox` rendering
- option buttons / reply box on the task card

## What this does not test

- full orchestrator pause/resume semantics
- `task.input_reply` acceptance and task continuation
- multiple pending input requests across multiple task ids

For those, use a real Opus task that calls `request_user_input(...)`.

---

## Required inputs

You need:

1. The desktop app running and connected to the live Gateway
2. The local desktop settings DB
3. SSH access to the VM
4. Redis running on the VM

You will fetch:

- `gatewayBaseUrl` from local desktop settings
- `gatewayApiToken` from local desktop settings
- the active desktop `channel` from Gateway `/channels/desktop/status`
- the current daily `session_id`

You will **not** need:

- the user's Cosmic API key
- the Telegram/WhatsApp bot tokens

---

## Step 1: Read the live desktop Gateway config from local SQLite

Run from the repo root:

```powershell
python -c "import sqlite3; db=r'C:\Users\Praveen Raj U S\Cosmic-OS\resources\user_data.db'; conn=sqlite3.connect(db); cur=conn.cursor(); cur.execute(\"select key, value from app_settings where key in ('gatewayBaseUrl','gatewayApiToken','desktopDeviceId') order by key\"); print(cur.fetchall())"
```

Expected keys:

- `gatewayBaseUrl`
- `gatewayApiToken`
- `desktopDeviceId`

Example shape:

```text
[
  ('desktopDeviceId', 'desk_a3f639dc299a4160'),
  ('gatewayApiToken', '...'),
  ('gatewayBaseUrl', 'http://3.137.194.119:8080')
]
```

---

## Step 2: Get the active desktop channel from the Gateway

Use the local API token from Step 1.

```powershell
python -c "import requests; url='http://3.137.194.119:8080/channels/desktop/status'; headers={'Authorization':'Bearer <GATEWAY_API_TOKEN>'}; r=requests.get(url,headers=headers,timeout=10); print(r.status_code); print(r.text)"
```

Expected response shape:

```json
{
  "platform": "desktop",
  "configured": true,
  "healthy": true,
  "last_error": null,
  "connection": {
    "status": "connected",
    "connection_count": 2,
    "channels": [
      "desktop:desk_a3f639dc299a4160",
      "desktop:desk_taskinputsmoke"
    ],
    "primary_channel": "desktop:desk_a3f639dc299a4160"
  }
}
```

Use:

- `connection.primary_channel`

That is the desktop channel that should receive the smoke request.

---

## Step 3: Determine the current daily session id

COSMIC uses shared daily sessions in the user's local timezone. For a quick smoke, verify the current daily session exists:

```powershell
python -c "import requests; base='http://3.137.194.119:8080'; headers={'Authorization':'Bearer <GATEWAY_API_TOKEN>'}; sid='sess_YYYYMMDD'; r=requests.get(f'{base}/sessions/{sid}',headers=headers,timeout=10); print(r.status_code); print(r.text[:200])"
```

Use the correct local-date session id, for example:

```text
sess_20260312
```

If you are unsure, check today's date in the user's local timezone and use `sess_YYYYMMDD`.

---

## Step 4: Create the one-off publisher script locally

Create a temporary file, for example `task_input_smoke.py`, with this content:

```python
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import redis


def main() -> int:
    if len(sys.argv) == 3:
        redis_url = sys.argv[1].strip()
        payload_path = Path(sys.argv[2]).expanduser()
        if not redis_url or not payload_path.exists():
            print("redis_url and payload file are required", file=sys.stderr)
            return 2
        payload = json.loads(payload_path.read_text(encoding="utf-8"))
        channel = str(payload.get("channel") or "").strip()
        session_id = str(payload.get("session_id") or "").strip()
        task_id = str(payload.get("task_id") or "").strip()
        question = str(payload.get("question") or "").strip()
        options = [str(item).strip() for item in payload.get("options", []) if str(item).strip()]
    elif len(sys.argv) >= 6:
        redis_url = sys.argv[1].strip()
        channel = sys.argv[2].strip()
        session_id = sys.argv[3].strip()
        task_id = sys.argv[4].strip()
        question = sys.argv[5].strip()
        options = [item.strip() for item in sys.argv[6:] if item.strip()]
    else:
        print(
            "usage: task_input_smoke.py <redis_url> <channel> <session_id> <task_id> <question> [option ...] OR task_input_smoke.py <redis_url> <payload.json>",
            file=sys.stderr,
        )
        return 2

    if not redis_url or not channel or not session_id or not task_id or not question:
        print("all required arguments must be non-empty", file=sys.stderr)
        return 2

    client = redis.from_url(redis_url, decode_responses=True)
    input_request_id = f"uir_smoke_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    payload = {
        "input_request_id": input_request_id,
        "task_id": task_id,
        "session_id": session_id,
        "agent": "cosmic/orchestrator:1.0.0",
        "channel": channel,
        "question": question,
        "options": options,
        "status": "pending",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    message_id = client.xadd(
        "user_input:requests",
        {"payload": json.dumps(payload, ensure_ascii=False)},
    )
    print(json.dumps({"ok": True, "message_id": message_id, "payload": payload}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

---

## Step 5: Create the payload file locally

Create `task_input_smoke_payload.json`:

```json
{
  "channel": "desktop:desk_a3f639dc299a4160",
  "session_id": "sess_20260312",
  "task_id": "tsk_ui_smoke_20260312",
  "question": "A background task needs your input. Which surface should COSMIC continue this work in?",
  "options": [
    "Task inbox",
    "Stay in chat"
  ]
}
```

Replace:

- `channel`
- `session_id`
- `task_id`
- `question`
- `options`

as needed.

---

## Step 6: Copy both files to the VM

```powershell
scp -i "C:\Users\Praveen Raj U S\Downloads\cosmic-vpc-feb-2026.pem" -o StrictHostKeyChecking=no `
  "C:\path\to\task_input_smoke.py" `
  "C:\path\to\task_input_smoke_payload.json" `
  ubuntu@3.137.194.119:/tmp/
```

---

## Step 7: Execute the publisher on the VM

Run it with the COSMIC backend virtualenv Python, because that environment already has `redis` installed:

```powershell
ssh -i "C:\Users\Praveen Raj U S\Downloads\cosmic-vpc-feb-2026.pem" -o StrictHostKeyChecking=no `
  ubuntu@3.137.194.119 `
  "~/Cosmic-OS/Backend/.venv/bin/python /tmp/task_input_smoke.py redis://127.0.0.1:6379/0 /tmp/task_input_smoke_payload.json"
```

Expected output shape:

```json
{
  "ok": true,
  "message_id": "1773370233406-0",
  "payload": {
    "input_request_id": "uir_smoke_20260313025033",
    "task_id": "tsk_ui_smoke_20260312",
    "session_id": "sess_20260312",
    "agent": "cosmic/orchestrator:1.0.0",
    "channel": "desktop:desk_a3f639dc299a4160",
    "question": "A background task needs your input. Which surface should COSMIC continue this work in?",
    "options": [
      "Task inbox",
      "Stay in chat"
    ],
    "status": "pending",
    "timestamp": "2026-03-13T02:50:33.403714Z"
  }
}
```

---

## Expected desktop behavior

If the desktop app is connected on the target channel:

1. A floating task interrupt should appear
2. It should **not** replace the current chat screen
3. Clicking `Reply` should open the `Task Inbox`
4. The same pending request should appear as a task card
5. The option buttons and freeform reply box should be visible

---

## Optional cleanup

Remove the temporary smoke files from the VM:

```powershell
ssh -i "C:\Users\Praveen Raj U S\Downloads\cosmic-vpc-feb-2026.pem" -o StrictHostKeyChecking=no `
  ubuntu@3.137.194.119 `
  "rm -f /tmp/task_input_smoke.py /tmp/task_input_smoke_payload.json"
```

---

## Notes

- This is a **Gateway-path smoke**. It uses the same Redis stream the orchestrator uses.
- It is safe for UI verification, but it is still synthetic:
  - there is no real active orchestrator run behind `task_id`
  - replying will test the desktop send path, but it will not resume a meaningful task unless the task id belongs to a real orchestrator run
- For a **full** pause/resume integration test, trigger a real Opus task that calls `request_user_input(...)` and then answer it from the desktop UI.

---

## Minimal recap

1. Read `gatewayBaseUrl`, `gatewayApiToken`, and `desktopDeviceId` from `resources/user_data.db`
2. Query `/channels/desktop/status`
3. Identify `primary_channel`
4. Determine the current `sess_YYYYMMDD`
5. Publish one `user_input:requests` event on the VM
6. Verify the floating interrupt + `Task Inbox` UI
