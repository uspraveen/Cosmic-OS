from __future__ import annotations

from pathlib import Path

import pytest

from agents.tabular_agent.sandbox import (
    bundle_script_with_prelude,
    persist_bundle_python_script,
    run_python_script,
    validate_tabular_python_code,
)


def test_validate_rejects_subprocess() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        validate_tabular_python_code("import duckdb\nimport subprocess\nsubprocess.run(['ls'])")


def test_validate_rejects_builtins_tamper() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        validate_tabular_python_code("__builtins__['open'] = 1")


def test_validate_allows_open_for_bundle_files() -> None:
    code = "open('bundle.duckdb', 'rb').read(1)\n"
    validate_tabular_python_code(code)


def test_validate_allows_duckdb_snippet() -> None:
    code = (
        "import duckdb\n"
        "con = duckdb.connect('bundle.duckdb')\n"
        "print(con.execute('select 1').fetchall())\n"
    )
    validate_tabular_python_code(code)


def test_persist_bundle_script_includes_prelude(tmp_path) -> None:
    code = "import duckdb\nprint(1)\n"
    p = persist_bundle_python_script(bundle_root=tmp_path, execution_id="exec_test_1", code=code)
    assert p.name == "exec_test_1.py"
    assert "codes" in p.parts
    text = p.read_text(encoding="utf-8")
    assert "COSMIC tabular sandbox prelude" in text
    assert "# --- user code ---" in text


def test_sandbox_blocks_escape_via_parent_path(tmp_path: Path) -> None:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope", encoding="utf-8")
    # cwd will be bundle_root; ../outside escapes
    user = (
        "try:\n"
        "    open('../outside/secret.txt')\n"
        "except PermissionError as e:\n"
        "    print('blocked')\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit(1)\n"
    )
    full = bundle_script_with_prelude(user_code=user)
    script = bundle / "codes" / "t_escape.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(full, encoding="utf-8")
    out = run_python_script(script_path=script, cwd=bundle, timeout_sec=10.0, bundle_root=bundle)
    assert out["exit_code"] == 0
    assert "blocked" in (out.get("stdout") or "")


def test_sandbox_blocks_absolute_path_outside_bundle(tmp_path: Path) -> None:
    bundle = tmp_path / "b"
    bundle.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    user = f"""
try:
    open(r'{secret}')
except PermissionError:
    print('blocked')
    raise SystemExit(0)
raise SystemExit(1)
"""
    full = bundle_script_with_prelude(user_code=user)
    script = bundle / "codes" / "t_abs.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(full, encoding="utf-8")
    out = run_python_script(script_path=script, cwd=bundle, timeout_sec=10.0, bundle_root=bundle)
    assert out["exit_code"] == 0
    assert "blocked" in (out.get("stdout") or "")
