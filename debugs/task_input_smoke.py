from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import redis


def main() -> int:
    if len(sys.argv) != 3:
        print('usage: task_input_smoke.py <redis_url> <payload.json>', file=sys.stderr)
        return 2
    redis_url = sys.argv[1].strip()
    payload_path = Path(sys.argv[2]).expanduser()
    payload = json.loads(payload_path.read_text(encoding='utf-8'))
    payload.setdefault('input_request_id', f"uir_smoke_{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}")
    payload.setdefault('agent', 'cosmic/orchestrator:1.0.0')
    payload.setdefault('status', 'pending')
    payload.setdefault('timestamp', datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
    client = redis.from_url(redis_url, decode_responses=True)
    message_id = client.xadd('user_input:requests', {'payload': json.dumps(payload, ensure_ascii=False)})
    print(json.dumps({'ok': True, 'message_id': message_id, 'payload': payload}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
