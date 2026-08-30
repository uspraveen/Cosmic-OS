from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.sandbox_permission_store import SandboxPermissionStore
from orchestrator.local_code_sandbox import (
    LocalCodeSandboxSettings,
    run_local_code_sandbox,
)
from orchestrator.sandbox_permissions import (
    build_sandbox_permission_receipt,
    capabilities_require_permission,
    normalize_requested_capabilities,
    session_grant_covers,
    union_session_grant,
)


def test_normalize_requested_capabilities_blocks_sensitive_paths() -> None:
    capabilities = normalize_requested_capabilities(
        {
            "network": True,
            "host_read_paths": ["/etc/passwd", "/home/ubuntu/project"],
            "allowed_hosts": ["Example.COM:443/path"],
        }
    )
    assert capabilities["network"] is True
    assert "/etc/passwd" not in capabilities["host_read_paths"]
    assert capabilities["allowed_hosts"] == ["example.com"]


def test_capabilities_require_permission_only_for_writes() -> None:
    assert capabilities_require_permission({"network": True}) is False
    assert capabilities_require_permission({"host_read_paths": ["/tmp"]}) is False
    assert capabilities_require_permission({"host_write_paths": ["/tmp"]}) is True


def test_sandbox_permission_store_round_trip(tmp_path: Path) -> None:
    store = SandboxPermissionStore(tmp_path / "sandbox_permissions.db")
    store.initialize()
    created = store.create_pending(
        {
            "description": "Read portfolio repo",
            "network": False,
            "host_read_paths": ["/home/ubuntu/Cosmic-OS"],
            "host_write_paths": [],
            "allowed_hosts": [],
            "code": "print('hello')",
            "packages": [],
            "timeout_sec": 30.0,
            "task_id": "task_123",
        }
    )
    permission_id = created["permission_id"]
    assert permission_id
    fetched = store.get(permission_id)
    assert fetched is not None
    assert fetched["status"] == "pending"
    assert fetched["code"] == "print('hello')"
    running = store.mark_running(permission_id)
    assert running is not None
    assert running["status"] == "running"
    assert store.mark_running(permission_id) is None
    executed = store.mark_executed(permission_id, {"status": "completed", "stdout": "hello\n"})
    assert executed is not None
    assert executed["status"] == "completed"
    assert executed["result"]["stdout"] == "hello\n"
    rejected_store = SandboxPermissionStore(tmp_path / "sandbox_permissions_reject.db")
    rejected_store.initialize()
    rejected_created = rejected_store.create_pending(
        {
            "description": "Reject me",
            "code": "print('x')",
            "task_id": "task_reject",
        }
    )
    rejected = rejected_store.mark_rejected(rejected_created["permission_id"], note="nope")
    assert rejected is not None
    assert rejected["status"] == "rejected"
    assert rejected["reviewer_note"] == "nope"


def test_sandbox_permission_store_list_for_scope(tmp_path: Path) -> None:
    store = SandboxPermissionStore(tmp_path / "sandbox_permissions.db")
    store.initialize()
    created = store.create_pending(
        {
            "description": "Probe repo",
            "code": "print('x')",
            "request_id": "req_abc",
            "task_id": "task_abc",
            "session_id": "sess_abc",
        }
    )
    permission_id = created["permission_id"]

    by_request = store.list_for_scope(request_id="req_abc")
    assert [p["permission_id"] for p in by_request] == [permission_id]

    # Falls back to task scope when request_id is unknown.
    by_task = store.list_for_scope(request_id="missing", task_id="task_abc")
    assert [p["permission_id"] for p in by_task] == [permission_id]

    # Falls back to session scope when request/task are unknown.
    by_session = store.list_for_scope(session_id="sess_abc")
    assert [p["permission_id"] for p in by_session] == [permission_id]

    assert store.list_for_scope(request_id="nope") == []
    assert store.list_for_scope() == []


def test_local_sandbox_blocks_os_import_without_grant(tmp_path: Path) -> None:
    result = run_local_code_sandbox(
        code="import os\nprint(os.getcwd())\n",
        artifacts_root=tmp_path / "artifacts",
        task_id="task_no_grant",
        settings=LocalCodeSandboxSettings(),
    )
    assert result.get("error") is True
    assert "os imports are blocked" in str(result.get("message"))


def test_local_sandbox_allows_os_for_granted_host_paths(tmp_path: Path) -> None:
    granted = tmp_path / "granted"
    granted.mkdir()
    (granted / "portfolio.html").write_text("PORTFOLIO-MARKER", encoding="utf-8")
    code = (
        "import os\n"
        f"target = {str(granted)!r}\n"
        "chunks = []\n"
        "for base, _dirs, files in os.walk(target):\n"
        "    for name in files:\n"
        "        with open(os.path.join(base, name), 'r', encoding='utf-8') as fh:\n"
        "            chunks.append(fh.read())\n"
        "print('READ:' + '|'.join(chunks))\n"
    )
    result = run_local_code_sandbox(
        code=code,
        artifacts_root=tmp_path / "artifacts",
        task_id="task_grant",
        settings=LocalCodeSandboxSettings(host_read_paths=(str(granted),)),
    )
    assert result.get("status") == "completed", result
    assert "PORTFOLIO-MARKER" in str(result.get("stdout"))


def test_local_sandbox_blocks_process_spawn_even_with_grant(tmp_path: Path) -> None:
    granted = tmp_path / "granted"
    granted.mkdir()
    result = run_local_code_sandbox(
        code="import os\nos.system('echo should-not-run')\n",
        artifacts_root=tmp_path / "artifacts",
        task_id="task_spawn",
        settings=LocalCodeSandboxSettings(host_read_paths=(str(granted),)),
    )
    assert result.get("status") == "failed", result
    assert "Process execution is blocked" in str(result.get("stderr"))


def test_local_sandbox_read_outside_grant_is_denied(tmp_path: Path) -> None:
    granted = tmp_path / "granted"
    granted.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET", encoding="utf-8")
    code = (
        "import os\n"
        f"secret = {str(secret)!r}\n"
        "try:\n"
        "    with open(secret, 'r', encoding='utf-8') as fh:\n"
        "        print('LEAK:' + fh.read())\n"
        "except PermissionError as exc:\n"
        "    print('BLOCKED:' + str(exc))\n"
    )
    result = run_local_code_sandbox(
        code=code,
        artifacts_root=tmp_path / "artifacts",
        task_id="task_outside",
        settings=LocalCodeSandboxSettings(host_read_paths=(str(granted),)),
    )
    assert result.get("status") == "completed", result
    stdout = str(result.get("stdout"))
    assert "TOP-SECRET" not in stdout
    assert "BLOCKED:" in stdout


def test_session_grant_covers_subset_capabilities() -> None:
    granted = union_session_grant(
        [
            {
                "network": True,
                "host_read_paths": ["/home/ubuntu", "/var/www"],
                "host_write_paths": ["/var/www"],
                "allowed_hosts": ["example.com"],
            }
        ]
    )
    assert granted is not None
    # Subset read path under a granted tree -> covered.
    assert session_grant_covers(
        {"network": False, "host_read_paths": ["/home/ubuntu/Cosmic-OS"], "host_write_paths": []},
        granted,
    )
    # Write under a granted write tree -> covered.
    assert session_grant_covers(
        {"network": True, "host_read_paths": [], "host_write_paths": ["/var/www/site"]},
        granted,
    )
    # Write under a read-only tree -> NOT covered.
    assert not session_grant_covers(
        {"network": False, "host_read_paths": [], "host_write_paths": ["/home/ubuntu/x"]},
        granted,
    )
    # Path outside any granted tree -> NOT covered.
    assert not session_grant_covers(
        {"network": False, "host_read_paths": ["/etc"], "host_write_paths": []},
        granted,
    )


def test_session_grant_requires_network_grant() -> None:
    granted = union_session_grant(
        [{"network": False, "host_read_paths": ["/srv"], "host_write_paths": []}]
    )
    assert granted is not None
    assert not session_grant_covers(
        {"network": True, "host_read_paths": ["/srv"], "host_write_paths": []},
        granted,
    )


def test_list_completed_for_session(tmp_path: Path) -> None:
    store = SandboxPermissionStore(tmp_path / "sandbox_permissions.db")
    store.initialize()
    created = store.create_pending(
        {
            "description": "Read repo",
            "code": "print('x')",
            "session_id": "sess_grant",
            "host_read_paths": ["/home/ubuntu"],
        }
    )
    permission_id = created["permission_id"]
    assert store.list_completed_for_session("sess_grant") == []
    store.mark_running(permission_id)
    store.mark_executed(permission_id, {"status": "completed", "stdout": "ok"})
    completed = store.list_completed_for_session("sess_grant")
    assert [p["permission_id"] for p in completed] == [permission_id]
    assert completed[0]["host_read_paths"] == ["/home/ubuntu"]


def test_build_sandbox_permission_receipt_shape() -> None:
    capabilities = normalize_requested_capabilities(
        {"network": True, "host_read_paths": ["/tmp/read"], "allowed_hosts": ["api.github.com"]}
    )
    receipt = build_sandbox_permission_receipt(
        permission_id="sbx_perm_test",
        description="Fetch remote data",
        capabilities=capabilities,
    )
    payload = json.loads(json.dumps(receipt))
    assert payload["permission_id"] == "sbx_perm_test"
    assert payload["network"] is True
    assert "api.github.com" in payload["summary"]


def test_local_sandbox_network_blocks_hosts_outside_allowlist(tmp_path: Path) -> None:
    code = (
        "import socket\n"
        "def probe(host):\n"
        "    try:\n"
        "        socket.getaddrinfo(host, 80)\n"
        "        print('ALLOWED:' + host)\n"
        "    except Exception as exc:\n"
        "        print('BLOCKED:' + host + ':' + str(exc))\n"
        "probe('example.com')\n"
        "probe('blocked.example')\n"
    )
    result = run_local_code_sandbox(
        code=code,
        artifacts_root=tmp_path / "artifacts",
        task_id="task_network_allowlist",
        settings=LocalCodeSandboxSettings(
            allow_network=True,
            allowed_hosts=("example.com",),
        ),
    )
    assert result.get("status") == "completed", result
    stdout = str(result.get("stdout"))
    assert "ALLOWED:example.com" in stdout
    assert "BLOCKED:blocked.example" in stdout


def test_session_grant_covers_network_host_subset() -> None:
    granted = union_session_grant(
        [{"network": True, "allowed_hosts": ["example.com"]}]
    )
    assert session_grant_covers(
        {"network": True, "allowed_hosts": ["example.com"]},
        granted,
    )
    assert not session_grant_covers(
        {"network": True, "allowed_hosts": ["api.github.com"]},
        granted,
    )
