from __future__ import annotations

import json
from pathlib import Path

import pytest

from gateway.sandbox_permission_store import SandboxPermissionStore
from orchestrator.sandbox_permissions import (
    build_sandbox_permission_receipt,
    capabilities_require_permission,
    normalize_requested_capabilities,
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


def test_capabilities_require_permission_when_network_or_host_paths() -> None:
    caps = normalize_requested_capabilities({"network": True})
    assert capabilities_require_permission(caps, settings_allow_network=False) is True
    assert capabilities_require_permission(caps, settings_allow_network=True) is False
    host_caps = normalize_requested_capabilities({"host_read_paths": ["/tmp"]})
    assert capabilities_require_permission(host_caps, settings_allow_network=False) is True


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
