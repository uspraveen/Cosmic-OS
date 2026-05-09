"""Persistent instruction files for the Alpha agent's CLI harnesses.

Codex CLI reads `$CODEX_HOME/AGENTS.md` (and any `AGENTS.md` walking up from
its cwd) on every run. Cursor CLI reads `$CURSOR_HOME/.cursor/rules/*.md`
(and per-project `.cursor/rules/*.md` or `.cursorrules`). This module is the
single source of truth for what those files contain on a COSMIC VM.

Two layers:

  * Global instructions — VM identity, capabilities, operating model,
    voice. Written into the Codex/Cursor home dirs once per run (idempotent),
    and at connect time so Day-1 sessions already have it.

  * Per-workspace instructions — project_registry-driven facts about the
    project the current task is touching. Dropped into the workspace root
    so Codex finds it via cwd-walk and Cursor finds it as a project rule.

The renderers are deliberately compact (target ~4 KB global, ~1 KB per
workspace). Long instruction files dilute the per-task prompt and burn
context budget.

Nothing in this module reads or writes secrets. The rendered files are
included in every CLI prompt — leaking credentials here would re-leak them
on every run.
"""
from __future__ import annotations

import logging
import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


# ── Runtime detection ────────────────────────────────────────────────────────


class RuntimeMode(str, Enum):
    HOST = "host"
    DOCKER = "docker"
    CODEX_SANDBOX_READ_ONLY = "codex_sandbox_read_only"
    CODEX_SANDBOX_WORKSPACE_WRITE = "codex_sandbox_workspace_write"
    CODEX_SANDBOX_FULL_ACCESS = "codex_sandbox_full_access"


_CODEX_SANDBOX_MODE_MAP = {
    "read-only": RuntimeMode.CODEX_SANDBOX_READ_ONLY,
    "workspace-write": RuntimeMode.CODEX_SANDBOX_WORKSPACE_WRITE,
    "danger-full-access": RuntimeMode.CODEX_SANDBOX_FULL_ACCESS,
}


def _is_in_docker() -> bool:
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return any(marker in cgroup for marker in ("docker", "containerd", "kubepods"))


def detect_runtime_mode(*, cli: str, codex_sandbox: str | None = None) -> RuntimeMode:
    """Detect the effective runtime the harness will execute under.

    Codex sandbox flags only apply to the codex CLI. For cursor, sandbox is
    not applicable — we report DOCKER if containerized, else HOST.
    """
    if cli == "codex" and codex_sandbox:
        mapped = _CODEX_SANDBOX_MODE_MAP.get(codex_sandbox.strip())
        if mapped is not None and mapped is not RuntimeMode.CODEX_SANDBOX_FULL_ACCESS:
            # Sandboxed modes are the dominant signal — surface them even inside Docker.
            return mapped
    if _is_in_docker():
        return RuntimeMode.DOCKER
    return RuntimeMode.HOST


# ── VM facts (best-effort, never raises) ─────────────────────────────────────


@dataclass(frozen=True)
class VmFacts:
    hostname: str
    primary_ip: str
    kernel: str
    os_release: str


def _primary_ip() -> str:
    """Best-effort detection of the VM's primary outbound IPv4."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            # Connecting a UDP socket doesn't actually send a packet — it just
            # asks the OS which interface would be used. 8.8.8.8 is a stable
            # public anchor; we never read or write through this socket.
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def _read_first_line(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except (OSError, IndexError):
        return ""


def collect_vm_facts() -> VmFacts:
    hostname = socket.gethostname() or "unknown"
    kernel = ""
    try:
        kernel = os.uname().release  # type: ignore[attr-defined]
    except AttributeError:
        kernel = ""
    os_release = ""
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("PRETTY_NAME="):
                os_release = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        os_release = ""
    return VmFacts(
        hostname=hostname,
        primary_ip=_primary_ip(),
        kernel=kernel,
        os_release=os_release,
    )


# ── Capability inventory ─────────────────────────────────────────────────────


# Tools we surface to Alpha by name + verb. Kept short on purpose — listing
# 50 binaries dilutes the signal. Each entry: (binary, "<one-line job>").
_CAPABILITY_PROBES: tuple[tuple[str, str], ...] = (
    ("bash", "shell scripting and pipelines"),
    ("python3", "general scripting, data, fallback for missing tools"),
    ("node", "JS execution and tooling"),
    ("git", "source control, including local repos"),
    ("curl", "HTTP requests, file downloads"),
    ("jq", "JSON processing on the command line"),
    ("yt-dlp", "download audio/video from YouTube and other sites"),
    ("ffmpeg", "audio/video transcode, trim, extract"),
    ("imagemagick", "image conversion (also try `convert` / `magick`)"),
    ("convert", "image conversion (ImageMagick)"),
    ("magick", "image conversion (ImageMagick 7)"),
    ("pandoc", "convert between markdown, HTML, docx, PDF"),
    ("pdftotext", "extract text from PDF"),
    ("rsync", "fast local or remote file sync"),
    ("docker", "container runtime"),
    ("supervisorctl", "control COSMIC's own service processes"),
    ("sqlite3", "inspect/modify SQLite DBs (gateway, ledger, registry)"),
    ("redis-cli", "inspect Redis (streams, keys, queues)"),
    ("lynx", "fetch and dump HTML as readable text"),
    ("pup", "CSS-selector parsing of HTML"),
)


def detect_capabilities() -> list[tuple[str, str]]:
    """Return ordered list of `(binary, blurb)` for tools that exist on PATH."""
    seen: set[str] = set()
    found: list[tuple[str, str]] = []
    for binary, blurb in _CAPABILITY_PROBES:
        if binary in seen:
            continue
        if shutil.which(binary):
            found.append((binary, blurb))
            seen.add(binary)
    return found


# ── Renderers ────────────────────────────────────────────────────────────────


_PERSONA_BLOCK = """\
You are **Alpha** — COSMIC's break-glass operator. Where the specialist
agents (slide, email, calendar, docs, slide, image, …) are scalpels for
single jobs, you are the workshop. When the user has a goal that doesn't
fit a packaged specialist, they reach for you. Real shell. Real
filesystem. Real git. No simulations.

You operate like Claude Code embedded inside COSMIC. You don't ask
permission for things you can already do — you do them, then report what
happened. Your value is that you actually finish things in the real
environment. A polite refusal is worse than a tried-and-failed attempt
with the actual error attached.
"""


_OPERATING_MODEL_BLOCK = """\
1. **Discover before assuming.** First moves on any non-trivial task:
   `pwd`, `ls -la`, `git status`, `which <tool>`. If the user mentions a
   path, `ls` it before asking about it. If they mention a service,
   `systemctl status` or `supervisorctl status` it. Reconnaissance is
   free; assumptions are expensive.
2. **Try before declining.** Never say "I can't do X" without proof.
   Run the command, report the actual error. The user can read your
   shell history.
3. **Direct beats indirect.** If the deploy target is on this same
   filesystem, write to it directly — do NOT reach for SSH, SCP, deploy
   keys, rsync-over-network. Use the local path. Localhost in this VM
   IS the production environment.
4. **Persistence with progress.** If approach A fails, approach B is
   informed by A's failure — not a repeat. No spinning. If you've tried
   three approaches and none worked, stop and report a real blocker.
5. **Honest narration.** Say what you actually did and what came back.
   Never invent success ("All done!" when nothing happened). Never
   redact failures into vague success language. The user trusts your
   shell output, not your prose.
6. **User corrections are durable.** If the user has told you something
   about their setup ("the site is on this VM", "deploy means rsync to
   /tmp/site"), treat it as a permanent fact, not a per-message hint.
   Don't make them say it twice.
7. **Concrete output over prose.** When you finish, lead with the
   artifact: the path, the diff, the URL, the command that reproduces
   it. Narration is secondary.
"""


_VOICE_BLOCK = """\
Terse. Operator-grade. State first, narrate second.

Good:
  "Wrote /tmp/site/posts.json (12 entries). Verified with `jq length`.
   nginx reloaded. Live at http://localhost/."

Bad:
  "Sure! I'd be happy to help with that. I'll start by carefully
   examining the project structure and then I will proceed to make the
   necessary updates to ensure everything works correctly..."

Skip "Sure!", "I'd be happy to", "I successfully completed", apology
padding, and meta-commentary about the task. Treat the user as a peer
engineer who can read a diff.
"""


_BLOCKED_BLOCK = """\
A blocker is concrete. State:
  • the goal you were trying to achieve
  • the specific commands you ran (last 3–5 are usually enough)
  • the exact error output (not a paraphrase)
  • what you've already ruled out

Do NOT say "I need credentials" or "I don't have access" without first
showing the user the command you tried and the error you got. The user
can fix the actual error. They can't fix the vague feeling.
"""


_GUARDRAILS_BLOCK = """\
Alpha has full system access by design. With that access:

  • Irreversible operations require an in-flight confirmation even if the
    parent task was pre-approved: `rm -rf` of anything outside the
    workspace, dropping a database, force-pushing a branch, deleting
    artifact directories, irreversible deploys, modifying anything under
    `/etc/cosmic/` or restarting `cosmic-*.service` units.
  • The other COSMIC services (gateway, orchestrator, agents) are running
    on this same VM. Don't restart or modify them unless the user goal
    explicitly asks for it.
  • Don't write secrets (API keys, tokens, SSH keys) into artifacts,
    workspaces, or your own output.
  • Every command you run is auditable. Behave accordingly.
"""


def _render_capability_lines(capabilities: list[tuple[str, str]]) -> str:
    if not capabilities:
        return "(no recognized tools detected — fall back to bash and python.)"
    return "\n".join(f"- `{binary}` — {blurb}" for binary, blurb in capabilities)


def _render_runtime_block(runtime_mode: RuntimeMode, vm_facts: VmFacts) -> str:
    runtime_line: str
    if runtime_mode is RuntimeMode.HOST:
        runtime_line = (
            "RUNTIME:    host (direct on the VM). You have full shell access. "
            "The same filesystem hosts the user's production projects."
        )
    elif runtime_mode is RuntimeMode.DOCKER:
        runtime_line = (
            "RUNTIME:    Docker container. Files you write to the workspace "
            "mount persist on the host; everything else is ephemeral. The host "
            "VM is the same machine where the user's projects are deployed."
        )
    elif runtime_mode is RuntimeMode.CODEX_SANDBOX_READ_ONLY:
        runtime_line = (
            "RUNTIME:    Codex sandbox = read-only. You CANNOT write to the "
            "filesystem. Use this for inspection, diagnostics, planning. "
            "Surface a clear blocker if the goal needs writes."
        )
    elif runtime_mode is RuntimeMode.CODEX_SANDBOX_WORKSPACE_WRITE:
        runtime_line = (
            "RUNTIME:    Codex sandbox = workspace-write. You can write inside "
            "the workspace dir; writes elsewhere will be denied. Network egress "
            "is restricted by the sandbox."
        )
    else:  # CODEX_SANDBOX_FULL_ACCESS
        runtime_line = (
            "RUNTIME:    Codex sandbox = danger-full-access. Treat this as "
            "host-equivalent power: you can write anywhere and reach the network."
        )
    return (
        f"{runtime_line}\n"
        f"HOSTNAME:   {vm_facts.hostname}\n"
        f"PRIMARY IP: {vm_facts.primary_ip}\n"
        f"OS:         {vm_facts.os_release or 'linux (unknown distro)'}\n"
        f"KERNEL:     {vm_facts.kernel or 'unknown'}\n"
        f"CRITICAL:   localhost on this machine == the user's production "
        f"environment. The COSMIC gateway, orchestrator, and every specialist "
        f"agent run on this same host. The user's deployed projects live on "
        f"this same filesystem. Do NOT default to SSH/SCP/rsync-over-network "
        f"for paths under /home, /var/www, /srv, /tmp, /opt — those are local."
    )


def _cli_identity_line(cli: str) -> str:
    if cli == "codex":
        return (
            "You are running as the **Codex CLI** (`codex exec`). Your prompts "
            "support reasoning effort and detailed tool plans. Use them."
        )
    if cli == "cursor":
        return (
            "You are running as the **Cursor CLI** (`cursor-agent`). You have "
            "the composer-2 model and Cursor's edit/diff tooling. Make use of "
            "patch-style edits where they fit."
        )
    return ""


def render_global_instructions(
    *,
    cli: str,
    codex_sandbox: str | None = None,
    runtime_mode: RuntimeMode | None = None,
    vm_facts: VmFacts | None = None,
    capabilities: list[tuple[str, str]] | None = None,
) -> str:
    """Render the deck-wide instructions for one CLI's home dir.

    Pure function — all detection inputs can be injected for tests.
    """
    rt = runtime_mode if runtime_mode is not None else detect_runtime_mode(
        cli=cli, codex_sandbox=codex_sandbox
    )
    facts = vm_facts if vm_facts is not None else collect_vm_facts()
    caps = capabilities if capabilities is not None else detect_capabilities()
    parts: list[str] = []
    parts.append("# Alpha — COSMIC Operator Instructions")
    parts.append(
        "_Auto-generated. Regenerated on each Alpha CLI run from VM state and the project registry. "
        "Edits to this file will be overwritten._"
    )
    parts.append("\n## 1 · Who you are\n")
    parts.append(_PERSONA_BLOCK)
    cli_line = _cli_identity_line(cli)
    if cli_line:
        parts.append(cli_line)
    parts.append("\n## 2 · Where you live\n")
    parts.append("```")
    parts.append(_render_runtime_block(rt, facts))
    parts.append("```")
    parts.append("\n## 3 · What you have\n")
    parts.append(
        "Tools detected on this VM. If you need something else, try installing "
        "it (apt/pip/npm) before declaring it unavailable."
    )
    parts.append("")
    parts.append(_render_capability_lines(caps))
    parts.append("\n## 4 · How you work\n")
    parts.append(_OPERATING_MODEL_BLOCK)
    parts.append("\n## 5 · How this user thinks\n")
    parts.append(
        "Per-project facts (deploy paths, framework, prior task summaries) "
        "live in the workspace's `AGENTS.md` — read it first when entering a "
        "workspace. If the workspace `AGENTS.md` and this file conflict, the "
        "workspace file wins (it is more specific)."
    )
    parts.append("\n## 6 · When blocked\n")
    parts.append(_BLOCKED_BLOCK)
    parts.append("\n## 7 · Guardrails\n")
    parts.append(_GUARDRAILS_BLOCK)
    parts.append("\n## 8 · How you sound\n")
    parts.append(_VOICE_BLOCK)
    return "\n".join(parts).rstrip() + "\n"


# ── Per-workspace overlay ────────────────────────────────────────────────────


def _truncate(text: str | None, *, limit: int) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def render_workspace_instructions(
    *,
    project: object | None,
    workspace_path: Path,
    artifacts_path: Path | None = None,
) -> str:
    """Render the per-workspace AGENTS.md.

    `project` is a `ProjectRecord`-shaped object (duck-typed to keep this
    module decoupled from `project_registry`). Missing fields render as
    placeholders so the file is still useful on first task.
    """

    def _attr(name: str) -> str:
        if project is None:
            return ""
        value = getattr(project, name, None)
        return _truncate(value if isinstance(value, str) else None, limit=400)

    def _attr_list(name: str) -> list[str]:
        if project is None:
            return []
        value = getattr(project, name, None)
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if isinstance(item, str) and item.strip()]

    project_id = _attr("project_id") or "(new)"
    aliases = _attr_list("aliases")
    repo_url = _attr("repo_url")
    deployment_url = _attr("deployment_url")
    last_task_id = _attr("last_task_id")
    summary = _truncate(_attr("summary"), limit=600)
    goal = _truncate(_attr("goal"), limit=800)
    context_brief = _truncate(_attr("context_brief"), limit=800)
    preferred_harness = _attr("preferred_harness")

    lines: list[str] = []
    lines.append("# Project Context for Alpha")
    lines.append(
        "_Per-workspace overlay. Generated from the COSMIC project registry. "
        "Read this BEFORE acting; the global Alpha instructions are the lower-priority default._"
    )
    lines.append("")
    lines.append(f"- **Project ID:** `{project_id}`")
    if aliases:
        lines.append(f"- **Aliases:** {', '.join(f'`{a}`' for a in aliases[:6])}")
    lines.append(f"- **Workspace path:** `{workspace_path}` _(your cwd for this task)_")
    if artifacts_path is not None:
        lines.append(f"- **Artifacts path:** `{artifacts_path}` _(write deliverables here; they are surfaced to the user)_")
    if repo_url:
        lines.append(f"- **Repository:** {repo_url}")
    if deployment_url:
        lines.append(f"- **Deployment URL:** {deployment_url}")
    if preferred_harness:
        lines.append(f"- **Preferred harness:** `{preferred_harness}`")
    if last_task_id:
        lines.append(f"- **Last task on this project:** `{last_task_id}` _(check its artifacts dir for prior work)_")
    lines.append("")
    if goal:
        lines.append("## Stated goal")
        lines.append("")
        lines.append(goal)
        lines.append("")
    if context_brief:
        lines.append("## Durable context (user-stated, treat as fact)")
        lines.append("")
        lines.append(context_brief)
        lines.append("")
    if summary and summary != goal:
        lines.append("## Last summary on this project")
        lines.append("")
        lines.append(summary)
        lines.append("")
    if not (goal or context_brief or summary):
        lines.append(
            "_No prior context recorded for this project yet. Run a quick recon "
            "(`pwd; ls -la; git status; cat README* 2>/dev/null`) before assuming layout._"
        )
        lines.append("")
    lines.append("## Operating reminders for THIS project")
    lines.append("")
    lines.append(
        "- The workspace path above is the cwd this run will start from. "
        "If the user's deployed copy lives elsewhere on this same VM, the "
        "**Deployment URL** or the durable context block tells you where."
    )
    lines.append(
        "- `cd` out of the workspace freely if the goal touches a path outside "
        "it — the workspace is your starting point, not a jail (subject to "
        "the runtime sandbox in the global instructions)."
    )
    lines.append(
        "- Persist anything the user should be able to download into the "
        "**Artifacts path** above. Files outside that dir are ephemeral from "
        "the user's perspective."
    )
    return "\n".join(lines).rstrip() + "\n"


# ── Idempotent writers ───────────────────────────────────────────────────────


CODEX_GLOBAL_INSTRUCTIONS_RELATIVE = "AGENTS.md"
CURSOR_GLOBAL_INSTRUCTIONS_RELATIVE = Path(".cursor") / "rules" / "cosmic.md"
WORKSPACE_INSTRUCTIONS_FILENAME = "AGENTS.md"


def _atomic_write_if_changed(path: Path, content: str) -> bool:
    """Write `content` to `path` if different from current. Returns True iff written."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        existing = None
    if existing == content:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)
    return True


def ensure_codex_global_instructions(
    *,
    codex_home: Path,
    codex_sandbox: str | None = None,
) -> dict[str, object]:
    """Idempotently write the Alpha AGENTS.md into `$CODEX_HOME`."""
    content = render_global_instructions(cli="codex", codex_sandbox=codex_sandbox)
    target = Path(codex_home).expanduser() / CODEX_GLOBAL_INSTRUCTIONS_RELATIVE
    try:
        wrote = _atomic_write_if_changed(target, content)
    except OSError:
        logger.exception("alpha.instructions.codex_write_failed path=%s", target)
        return {"path": str(target), "wrote": False, "error": True}
    return {"path": str(target), "wrote": wrote, "bytes": len(content.encode("utf-8"))}


def ensure_cursor_global_instructions(
    *,
    cursor_home: Path,
) -> dict[str, object]:
    """Idempotently write the Alpha cosmic.md into `$CURSOR_HOME/.cursor/rules/`."""
    content = render_global_instructions(cli="cursor")
    target = Path(cursor_home).expanduser() / CURSOR_GLOBAL_INSTRUCTIONS_RELATIVE
    try:
        wrote = _atomic_write_if_changed(target, content)
    except OSError:
        logger.exception("alpha.instructions.cursor_write_failed path=%s", target)
        return {"path": str(target), "wrote": False, "error": True}
    return {"path": str(target), "wrote": wrote, "bytes": len(content.encode("utf-8"))}


def ensure_alpha_global_instructions(
    *,
    codex_home: Path | None = None,
    cursor_home: Path | None = None,
    codex_sandbox: str | None = None,
) -> dict[str, dict[str, object]]:
    """Write whichever home(s) the caller passes. Idempotent and never raises.

    Designed to be safe to call once per CLI invocation — same content
    produces no disk write, different content (e.g. the user changed VM)
    triggers an atomic rewrite.
    """
    result: dict[str, dict[str, object]] = {}
    if codex_home is not None:
        result["codex"] = ensure_codex_global_instructions(
            codex_home=codex_home, codex_sandbox=codex_sandbox
        )
    if cursor_home is not None:
        result["cursor"] = ensure_cursor_global_instructions(cursor_home=cursor_home)
    return result


def seed_workspace_instructions(
    *,
    workspace_path: Path,
    artifacts_path: Path | None = None,
    project: object | None = None,
) -> dict[str, object]:
    """Write the per-workspace AGENTS.md. Idempotent."""
    content = render_workspace_instructions(
        project=project,
        workspace_path=workspace_path,
        artifacts_path=artifacts_path,
    )
    target = Path(workspace_path) / WORKSPACE_INSTRUCTIONS_FILENAME
    try:
        wrote = _atomic_write_if_changed(target, content)
    except OSError:
        logger.exception(
            "alpha.instructions.workspace_write_failed path=%s", target
        )
        return {"path": str(target), "wrote": False, "error": True}
    return {"path": str(target), "wrote": wrote, "bytes": len(content.encode("utf-8"))}
