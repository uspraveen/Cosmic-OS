from __future__ import annotations

import pytest

from agents.tabular_agent.prompt_assets import build_internal_context

# Unique to fpna_supplement in tabular_staged_context.md (not in shared/summarize/plan/execute).
_FPNA_ONLY_PHRASE = "like-for-like period lengths"


def test_summarize_default_guidance_only() -> None:
    ctx = build_internal_context("summarize", include_fpna=False)
    assert "v1 header heuristic" in ctx
    assert "preview excerpt" in ctx.lower() or "preview excerpts" in ctx.lower()
    assert _FPNA_ONLY_PHRASE not in ctx


def test_fpna_pack_optional_summarize() -> None:
    off = build_internal_context("summarize", include_fpna=False)
    on = build_internal_context("summarize", include_fpna=True)
    assert _FPNA_ONLY_PHRASE not in off
    assert _FPNA_ONLY_PHRASE in on
    assert len(on) > len(off)


def test_stage_specific_composition() -> None:
    s = build_internal_context("summarize", include_fpna=False)
    p = build_internal_context("plan", include_fpna=False)
    e = build_internal_context("execute", include_fpna=False)
    assert "MiMo" in s or "internal" in s.lower()
    assert "planner" in p.lower() or "query" in p.lower()
    assert "execution" in e.lower() or "validator" in e.lower() or "validate" in e.lower()
    assert s != p != e


def test_execute_never_includes_fpna_even_when_requested() -> None:
    ctx = build_internal_context("execute", include_fpna=True)
    assert _FPNA_ONLY_PHRASE not in ctx


def test_plan_includes_fpna_when_flag() -> None:
    assert _FPNA_ONLY_PHRASE in build_internal_context("plan", include_fpna=True)
    assert _FPNA_ONLY_PHRASE not in build_internal_context("plan", include_fpna=False)


def test_no_duplicate_paragraph_blocks() -> None:
    """Shared + stage (+ fpna) must not repeat the same paragraph blob."""
    for stage in ("summarize", "plan", "execute"):
        for fpna in (False, True):
            ctx = build_internal_context(stage, include_fpna=fpna)  # type: ignore[arg-type]
            paras = [p.strip() for p in ctx.split("\n\n") if p.strip()]
            assert len(paras) == len(set(paras)), f"duplicate paragraph in {stage} fpna={fpna}"


def test_no_unintended_prompt_bloat_repeated_headers() -> None:
    """Stage body headings should not appear twice (simple bloat guard)."""
    ctx = build_internal_context("summarize", include_fpna=True)
    assert ctx.count("## ") == 0  # composer strips file headers; output is plain text


def test_unknown_stage_raises() -> None:
    with pytest.raises(ValueError, match="Unknown tabular internal stage"):
        build_internal_context("not_a_stage")  # type: ignore[arg-type]
