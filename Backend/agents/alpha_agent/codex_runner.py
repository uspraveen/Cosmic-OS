from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from shared.contracts import ArtifactManifest

from .config import AlphaAgentConfig
from .workspace_manager import WorkspacePaths


SECRET_PATTERN = re.compile(r"sk-[^\s\"']{4,}")


def _redact(value: str) -> str:
    return SECRET_PATTERN.sub("sk-...redacted", value)


def _tail(value: str, limit: int = 8000) -> str:
    redacted = _redact(value or "")
    return redacted[-limit:]


@dataclass(frozen=True)
class CodexRunResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    command: list[str]
    last_message_path: Path
    last_message: str
    duration_sec: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "returncode": self.returncode,
            "stdout_tail": _tail(self.stdout, 4000),
            "stderr_tail": _tail(self.stderr, 4000),
            "timed_out": self.timed_out,
            "last_message_path": str(self.last_message_path),
            "last_message": _tail(self.last_message, 12000),
            "duration_sec": round(self.duration_sec, 3),
            "command": self.safe_command(),
        }

    def safe_command(self) -> list[str]:
        safe: list[str] = []
        redact_next = False
        for part in self.command:
            if redact_next:
                safe.append("<redacted>")
                redact_next = False
                continue
            safe.append(part)
            if part in {"--model", "-m"}:
                redact_next = False
        return safe


class CodexWorkspaceRunner:
    def __init__(self, config: AlphaAgentConfig) -> None:
        self.config = config

    def codex_binary(self) -> str | None:
        return shutil.which("codex")

    def is_available(self) -> bool:
        return self.codex_binary() is not None

    def build_command(
        self,
        *,
        paths: WorkspacePaths,
        prompt: str,
        output_path: Path,
        model: str | None = None,
        sandbox: str | None = None,
        json_events: bool = False,
    ) -> list[str]:
        binary = self.codex_binary() or "codex"
        command = [
            binary,
            "exec",
            "--cd",
            str(paths.workspace),
            "--sandbox",
            sandbox or self.config.codex_sandbox,
            "--skip-git-repo-check",
            "--output-last-message",
            str(output_path),
        ]
        if json_events:
            command.append("--json")
        if model:
            command.extend(["--model", model])
        command.append("-")
        return command

    async def run(
        self,
        *,
        paths: WorkspacePaths,
        prompt: str,
        model: str | None = None,
        sandbox: str | None = None,
        timeout_sec: float | None = None,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> CodexRunResult:
        paths.workspace.mkdir(parents=True, exist_ok=True)
        paths.artifacts.mkdir(parents=True, exist_ok=True)
        self.config.codex_home.mkdir(parents=True, exist_ok=True)
        output_path = paths.artifacts / "codex-last-message.md"
        command = self.build_command(
            paths=paths,
            prompt=prompt,
            output_path=output_path,
            model=model,
            sandbox=sandbox,
            json_events=event_callback is not None,
        )
        started_at = asyncio.get_running_loop().time()
        if not self.is_available():
            return CodexRunResult(
                returncode=127,
                stdout="",
                stderr="codex executable not found",
                timed_out=False,
                command=command,
                last_message_path=output_path,
                last_message="",
                duration_sec=0.0,
            )

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
        )
        timed_out = False
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        try:
            stdout_parts, stderr_parts = await asyncio.wait_for(
                self._communicate_streaming(
                    process,
                    prompt=prompt,
                    event_callback=event_callback,
                ),
                timeout=timeout_sec or self.config.codex_timeout_sec,
            )
        except asyncio.TimeoutError:
            timed_out = True
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout_parts.append(stdout_bytes.decode("utf-8", errors="replace"))
            stderr_parts.append(stderr_bytes.decode("utf-8", errors="replace"))
        except asyncio.CancelledError:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.communicate(), timeout=5.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
            raise

        duration_sec = asyncio.get_running_loop().time() - started_at
        stdout = "".join(stdout_parts)
        stderr = "".join(stderr_parts)
        last_message = ""
        if output_path.exists():
            last_message = output_path.read_text(encoding="utf-8", errors="replace")
        return CodexRunResult(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            command=command,
            last_message_path=output_path,
            last_message=last_message,
            duration_sec=duration_sec,
        )

    async def _communicate_streaming(
        self,
        process: asyncio.subprocess.Process,
        *,
        prompt: str,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[list[str], list[str]]:
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []

        async def emit(entry: dict[str, Any]) -> None:
            if event_callback is None:
                return
            try:
                await event_callback(entry)
            except Exception:
                # Terminal streaming must not fail the Codex run.
                return

        async def write_stdin() -> None:
            if process.stdin is None:
                return
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
            process.stdin.close()
            try:
                await process.stdin.wait_closed()
            except (AttributeError, BrokenPipeError, ConnectionResetError):
                return

        async def read_stdout() -> None:
            if process.stdout is None:
                return
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                stdout_parts.append(text)
                if event_callback is not None:
                    await emit(self._codex_json_line_to_terminal_event(text, stream="stdout"))

        async def read_stderr() -> None:
            if process.stderr is None:
                return
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace")
                stderr_parts.append(text)
                if event_callback is not None:
                    await emit({
                        "stream": "stderr",
                        "event_type": "stderr",
                        "text": _tail(text.strip(), 2000),
                    })

        await asyncio.gather(write_stdin(), read_stdout(), read_stderr())
        await process.wait()
        return stdout_parts, stderr_parts

    def _codex_json_line_to_terminal_event(self, line: str, *, stream: str) -> dict[str, Any]:
        text = line.strip()
        if not text:
            return {"stream": stream, "event_type": "empty", "text": ""}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"stream": stream, "event_type": "stdout", "text": _tail(text, 2000)}
        if not isinstance(payload, dict):
            return {"stream": stream, "event_type": "json", "text": _tail(text, 2000)}

        event_type = str(payload.get("type") or payload.get("event") or "codex.event")
        message = self._extract_codex_event_text(payload)
        detail = self._extract_codex_event_detail(payload)
        return {
            "stream": stream,
            "event_type": event_type,
            "text": _tail(message or event_type, 2000),
            "detail": _tail(detail, 2000) if detail else None,
        }

    def _extract_codex_event_text(self, payload: dict[str, Any]) -> str:
        for key in ("message", "text", "content", "delta", "summary"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        item = payload.get("item")
        if isinstance(item, dict):
            for key in ("message", "text", "content", "command", "summary"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        command = payload.get("command")
        if isinstance(command, list):
            return " ".join(str(part) for part in command if str(part).strip())
        if isinstance(command, str) and command.strip():
            return command.strip()
        return str(payload.get("type") or "codex.event")

    def _extract_codex_event_detail(self, payload: dict[str, Any]) -> str | None:
        for key in ("status", "cwd", "path", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return f"{key}: {value.strip()}"
        return None

    def artifact_for_last_message(self, *, task_id: str, result: CodexRunResult) -> ArtifactManifest | None:
        path = result.last_message_path
        if not path.exists() or not result.last_message.strip():
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return ArtifactManifest(
            artifact_id=f"art_alpha_codex_{digest[:16]}",
            task_id=task_id,
            mime="text/markdown",
            sha256=digest,
            path=str(path),
            created_by_agent="cosmic/alpha-agent:1.0.0",
            kind="output",
            audience="deliverable",
        )

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["CODEX_HOME"] = str(self.config.codex_home)
        env.setdefault("HOME", str(self.config.codex_home.parent))
        return env
