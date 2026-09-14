"""The Alpha global instructions must state the actual GitHub grant.

§4 lists the connected repositories and the one boundary that matters: the
connector is repo-scoped, so creating a new repo or pushing elsewhere cannot
work. Before this, the section described the checkout layout generically and
Alpha had to discover the grant by failing against it.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agents.alpha_agent.instructions import (  # noqa: E402
    RuntimeMode,
    VmFacts,
    ensure_codex_global_instructions,
    render_global_instructions,
)

_VM_FACTS = VmFacts(
    hostname="test-vm",
    primary_ip="10.0.0.1",
    kernel="6.1.0",
    os_release="Ubuntu 24.04",
)


def _render(**overrides) -> str:
    params = {
        "cli": "codex",
        "runtime_mode": RuntimeMode.HOST,
        "vm_facts": _VM_FACTS,
        "capabilities": [],
    }
    params.update(overrides)
    return render_global_instructions(**params)


def test_connected_repos_are_listed_with_the_scope_boundary() -> None:
    text = _render(connected_repos=["uspraveen/uspraveen.github.io"])

    assert "Connected right now: `uspraveen/uspraveen.github.io`" in text
    assert "repo-scoped" in text
    assert "creating a new repository or pushing anywhere else will fail" in text


def test_an_empty_grant_says_so() -> None:
    text = _render(connected_repos=[])

    assert "no repositories are connected" in text
    assert "repo-scoped" in text


def test_no_repo_data_keeps_the_legacy_generic_section() -> None:
    text = _render()

    assert "Connected right now" not in text
    assert "Connected GitHub repositories live under a single canonical checkout root" in text


def test_codex_writer_persists_the_grant(tmp_path: Path) -> None:
    result = ensure_codex_global_instructions(
        codex_home=tmp_path,
        connected_repos=["uspraveen/uspraveen.github.io"],
    )

    assert result["wrote"] is True
    written = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "uspraveen/uspraveen.github.io" in written
