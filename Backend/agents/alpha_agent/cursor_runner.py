from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from shared.contracts import ArtifactManifest

from .config import AlphaAgentConfig
from .workspace_manager import WorkspacePaths


SECRET_PATTERN = re.compile(r"(sk-[^\s\"']{4,}|cursor_[^\s\"']{8,})", re.IGNORECASE)


def _redact(value: str) -> str:
    return SECRET_PATTERN.sub("<redacted>", value)


def _tail(value: str, limit: int = 8000) -> str:
    redacted = _redact(value or "")
    return redacted[-limit:]


def find_cursor_agent_binary() -> str | None:
    binary = shutil.which("cursor-agent")
    if binary:
        return binary
    names = ["cursor-agent.exe", "cursor-agent.cmd", "cursor-agent"] if os.name == "nt" else ["cursor-agent"]
    candidates: list[Path] = []
    for name in names:
        candidates.extend(
            [
                Path.home() / ".local" / "bin" / name,
                Path("/usr/local/bin") / name,
                Path("/usr/bin") / name,
                Path("/home/ubuntu/.local/bin") / name,
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None


@dataclass(frozen=True)
class CursorRunResult:
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
            "command": list(self.command),
        }


class CursorWorkspaceRunner:
    def __init__(self, config: AlphaAgentConfig) -> None:
        self.config = config

    def cursor_binary(self) -> str | None:
        return find_cursor_agent_binary()

    def is_available(self) -> bool:
        return self.cursor_binary() is not None

    def build_command(
        self,
        *,
        paths: WorkspacePaths,
        prompt: str,
        model: str | None = None,
        stream_json: bool = False,
    ) -> list[str]:
        binary = self.cursor_binary() or "cursor-agent"
        command = [
            binary,
            "--print",
            "--force",
            "--output-format",
            "stream-json" if stream_json else "json",
        ]
        if model:
            command.extend(["--model", model])
        command.append(prompt)
        return command

    async def run(
        self,
        *,
        paths: WorkspacePaths,
        prompt: str,
        model: str | None = None,
        timeout_sec: float | None = None,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> CursorRunResult:
        paths.workspace.mkdir(parents=True, exist_ok=True)
        paths.artifacts.mkdir(parents=True, exist_ok=True)
        self.config.cursor_home.mkdir(parents=True, exist_ok=True)
        output_path = paths.artifacts / "cursor-last-message.md"
        command = self.build_command(
            paths=paths,
            prompt=prompt,
            model=model,
            stream_json=event_callback is not None,
        )
        started_at = asyncio.get_running_loop().time()
        if not self.is_available():
            return CursorRunResult(
                returncode=127,
                stdout="",
                stderr="cursor-agent executable not found",
                timed_out=False,
                command=command,
                last_message_path=output_path,
                last_message="",
                duration_sec=0.0,
            )

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(paths.workspace),
            env=self._env(),
        )
        timed_out = False
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        try:
            stdout_parts, stderr_parts = await asyncio.wait_for(
                self._communicate_streaming(
                    process,
                    event_callback=event_callback,
                ),
                timeout=timeout_sec or self.config.cursor_timeout_sec,
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
        last_message = self._extract_last_message(stdout)
        if last_message:
            output_path.write_text(last_message, encoding="utf-8")
        return CursorRunResult(
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
                    await emit(self._cursor_json_line_to_terminal_event(text, stream="stdout"))

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

        await asyncio.gather(read_stdout(), read_stderr())
        await process.wait()
        return stdout_parts, stderr_parts

    def _cursor_json_line_to_terminal_event(self, line: str, *, stream: str) -> dict[str, Any]:
        text = line.strip()
        if not text:
            return {"stream": stream, "event_type": "empty", "text": ""}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"stream": stream, "event_type": "stdout", "text": _tail(text, 2000)}
        if not isinstance(payload, dict):
            return {"stream": stream, "event_type": "json", "text": _tail(text, 2000)}

        event_type = str(payload.get("type") or "cursor.event")
        message = self._extract_cursor_event_text(payload)
        detail = self._extract_cursor_event_detail(payload)
        return {
            "stream": stream,
            "event_type": event_type,
            "text": _tail(message or event_type, 2000),
            "detail": _tail(detail, 2000) if detail else None,
        }

    def _extract_cursor_event_text(self, payload: dict[str, Any]) -> str:
        event_type = str(payload.get("type") or "cursor.event")
        if event_type == "system":
            return "Cursor Agent initialized"
        if event_type == "user":
            return "User prompt submitted"
        if event_type == "assistant":
            message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            content = message.get("content") if isinstance(message.get("content"), list) else []
            chunks = [
                str(item.get("text") or "").strip()
                for item in content
                if isinstance(item, dict) and str(item.get("text") or "").strip()
            ]
            return "".join(chunks).strip() or "Assistant response"
        if event_type == "tool_call":
            return self._extract_tool_call_label(payload)
        if event_type == "result":
            return "Cursor Agent completed task"
        return event_type

    def _extract_cursor_event_detail(self, payload: dict[str, Any]) -> str | None:
        details: list[str] = []
        for key in ("subtype", "cwd", "model", "permissionMode", "apiKeySource"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                details.append(f"{key}: {value.strip()}")
        if details:
            return "; ".join(details)
        return None

    def _extract_tool_call_label(self, payload: dict[str, Any]) -> str:
        subtype = str(payload.get("subtype") or "").strip()
        tool_call = payload.get("tool_call") if isinstance(payload.get("tool_call"), dict) else {}
        if not tool_call:
            return f"Tool call {subtype or 'event'}"
        tool_name = next(iter(tool_call.keys()))
        label = re.sub(r"([a-z])([A-Z])", r"\1 \2", tool_name).replace("Tool Call", "").strip()
        args = tool_call.get(tool_name, {}).get("args") if isinstance(tool_call.get(tool_name), dict) else {}
        if isinstance(args, dict):
            for key in ("path", "command", "cmd", "query"):
                value = args.get(key)
                if isinstance(value, str) and value.strip():
                    return f"{label}: {value.strip()}"
        return label or f"Tool call {subtype or 'event'}"

    def _extract_last_message(self, stdout: str) -> str:
        fallback_chunks: list[str] = []
        for line in stdout.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "result" and isinstance(payload.get("result"), str):
                return payload["result"].strip()
            if payload.get("type") == "assistant":
                message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
                content = message.get("content") if isinstance(message.get("content"), list) else []
                fallback_chunks.extend(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict) and str(item.get("text") or "")
                )
        return "".join(fallback_chunks).strip()

    def artifact_for_last_message(self, *, task_id: str, result: CursorRunResult) -> ArtifactManifest | None:
        path = result.last_message_path
        if not path.exists() or not result.last_message.strip():
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return ArtifactManifest(
            artifact_id=f"art_alpha_cursor_{digest[:16]}",
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
        env["HOME"] = str(self.config.cursor_home)
        env["CURSOR_AGENT"] = "1"
        existing_path = env.get("PATH", "")
        local_bin = str(Path.home() / ".local" / "bin")
        env["PATH"] = f"{local_bin}{os.pathsep}/usr/local/bin{os.pathsep}{existing_path}"
        return env
