from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path
from uuid import uuid4

import pytest

from agents.slide_agent.agent_tools import PythonSandboxRunner, SandboxConfig, ToolContext, ToolExecutionError
from agents.slide_agent.sandbox import validate_slide_python_code


TEST_RUNTIME_ROOT = Path(__file__).resolve().parent / "_runtime"


def _runtime_dir() -> Path:
    path = TEST_RUNTIME_ROOT / uuid4().hex
    path.mkdir(parents=True, exist_ok=False)
    return path


def _config() -> SandboxConfig:
    return SandboxConfig(
        timeout_sec=10,
        max_files=4,
        max_bytes_per_file=1_000_000,
        max_script_bytes=256_000,
        allow_network=False,
        allow_pip=False,
        pip_timeout_sec=30,
        venv_cache_root="",
    )


def test_python_sandbox_generates_file_and_receipt() -> None:
    runtime_dir = _runtime_dir()
    try:
        result = PythonSandboxRunner(_config()).run(
            {
                "purpose": "unit_test_chart",
                "code": (
                    "from pathlib import Path\n"
                    "Path('chart.txt').write_text('ok', encoding='utf-8')\n"
                    "print('created chart')\n"
                ),
            },
            ToolContext(output_dir=runtime_dir / "out"),
        )

        assert result["status"] == "completed"
        assert result["return_code"] == 0
        assert result["files"][0]["filename"] == "chart.txt"
        assert Path(result["generated_assets"][0]["path"]).read_text(encoding="utf-8") == "ok"
        assert Path(result["receipt_path"]).is_file()
        assert result["network_enabled"] is False
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_python_sandbox_blocks_path_escape() -> None:
    runtime_dir = _runtime_dir()
    try:
        with pytest.raises(ToolExecutionError, match="path escapes sandbox root"):
            PythonSandboxRunner(_config()).run(
                {
                    "purpose": "escape_attempt",
                    "code": (
                        "from pathlib import Path\n"
                        "Path('..').joinpath('escape.txt').write_text('bad', encoding='utf-8')\n"
                    ),
                },
                ToolContext(output_dir=runtime_dir / "out"),
            )
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_python_sandbox_allows_matplotlib_chart_when_installed() -> None:
    if importlib.util.find_spec("matplotlib") is None:
        pytest.skip("matplotlib is not installed")
    runtime_dir = _runtime_dir()
    try:
        result = PythonSandboxRunner(_config()).run(
            {
                "purpose": "matplotlib_chart",
                "code": (
                    "import matplotlib.pyplot as plt\n"
                    "fig, ax = plt.subplots(figsize=(4, 3))\n"
                    "ax.plot([1, 2, 3], [2, 5, 3])\n"
                    "ax.set_title('Sandbox chart')\n"
                    "fig.savefig('chart.png', dpi=120)\n"
                ),
            },
            ToolContext(output_dir=runtime_dir / "out"),
        )

        assert result["status"] == "completed"
        assert result["files"][0]["filename"] == "chart.png"
        assert Path(result["generated_assets"][0]["path"]).stat().st_size > 0
    finally:
        shutil.rmtree(runtime_dir, ignore_errors=True)


def test_validate_slide_python_code_blocks_subprocess() -> None:
    with pytest.raises(ValueError, match="forbidden construct"):
        validate_slide_python_code("import subprocess\nsubprocess.run(['echo', 'bad'])")
