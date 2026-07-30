from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from shared.cursor_cli_config import ensure_cursor_cli_non_fast_config
from shared.contracts import ArtifactManifest

from .config import AlphaAgentConfig
from .instructions import ensure_cursor_global_instructions
from .streaming import compact_for_memory, iter_stream_lines
from .workspace_manager import WorkspacePaths


SECRET_PATTERN = re.compile(r"(sk-[^\s\"']{4,}|cursor_[^\s\"']{8,})", re.IGNORECASE)
CURSOR_MODEL_ALIASES = {
    "composer": "composer-2.5",
    "composer normal": "composer-2.5",
    "normal composer": "composer-2.5",
    "composer 2": "composer-2.5",
    "composer2": "composer-2.5",
    "composer-2": "composer-2.5",
    "composer-2-normal": "composer-2.5",
    "composer 2 normal": "composer-2.5",
    "normal composer 2": "composer-2.5",
    "composer 2.5": "composer-2.5",
    "composer2.5": "composer-2.5",
    "composer-2.5": "composer-2.5",
    "composer-2.5-normal": "composer-2.5",
    "composer 2.5 normal": "composer-2.5",
    "normal composer 2.5": "composer-2.5",
    # Cursor Grok 4.5 — High effort, not Fast (effort/fast are part of the CLI model id).
    "grok": "cursor-grok-4.5-high",
    "grok 4.5": "cursor-grok-4.5-high",
    "grok4.5": "cursor-grok-4.5-high",
    "grok-4.5": "cursor-grok-4.5-high",
    "grok-4.5-high": "cursor-grok-4.5-high",
    "cursor grok": "cursor-grok-4.5-high",
    "cursor-grok": "cursor-grok-4.5-high",
    "cursor grok 4.5": "cursor-grok-4.5-high",
    "cursor-grok-4.5": "cursor-grok-4.5-high",
    "cursor-grok-4.5-high": "cursor-grok-4.5-high",
    "cursor grok 4.5 high": "cursor-grok-4.5-high",
}


def _redact(value: str) -> str:
    return SECRET_PATTERN.sub("<redacted>", value)


def _tail(value: str, limit: int = 8000) -> str:
    redacted = _redact(value or "")
    return redacted[-limit:]


def normalize_cursor_model(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    if not normalized or normalized.lower() == "auto":
        return None
    alias_key = re.sub(r"\s+", " ", normalized.lower())
    return CURSOR_MODEL_ALIASES.get(alias_key, normalized)


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
    requested_model: str | None = None
    observed_model: str | None = None
    native_session_id: str | None = None
    resume_session_id: str | None = None
    resume_used: bool = False
    model_mismatch: bool = False
    cancelled: bool = False
    init_timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.model_mismatch and not self.cancelled

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
            "requested_model": self.requested_model,
            "observed_model": self.observed_model,
            "native_session_id": self.native_session_id,
            "resume_session_id": self.resume_session_id,
            "resume_used": self.resume_used,
            "model_mismatch": self.model_mismatch,
            "cancelled": self.cancelled,
            "init_timed_out": self.init_timed_out,
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
        resume_chat_id: str | None = None,
        stream_json: bool = False,
    ) -> list[str]:
        binary = self.cursor_binary() or "cursor-agent"
        command = [
            binary,
            "--print",
            "--force",
            "--trust",
            "--sandbox",
            "disabled",
            "--output-format",
            "stream-json" if stream_json else "json",
        ]
        normalized_resume = str(resume_chat_id or "").strip()
        if normalized_resume:
            command.extend(["--resume", normalized_resume])
        normalized_model = normalize_cursor_model(model)
        if normalized_model:
            command.extend(["--model", normalized_model])
        command.append(prompt)
        return command

    async def create_chat(self, *, paths: WorkspacePaths) -> str | None:
        binary = self.cursor_binary()
        if not binary:
            return None
        self.config.cursor_home.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            binary,
            "create-chat",
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(paths.workspace),
            env=self._env(),
            **self._process_group_kwargs(),
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=30.0)
        except asyncio.TimeoutError:
            self._terminate_process_tree(process)
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._kill_process_tree(process)
                await process.wait()
            return None
        if process.returncode != 0:
            return None
        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        return self._parse_chat_id(stdout) or self._parse_chat_id(stderr)

    async def run(
        self,
        *,
        paths: WorkspacePaths,
        prompt: str,
        model: str | None = None,
        resume_chat_id: str | None = None,
        timeout_sec: float | None = None,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> CursorRunResult:
        paths.workspace.mkdir(parents=True, exist_ok=True)
        paths.artifacts.mkdir(parents=True, exist_ok=True)
        self.config.cursor_home.mkdir(parents=True, exist_ok=True)
        config_warning = ""
        try:
            ensure_cursor_cli_non_fast_config(self.config.cursor_home)
        except OSError as exc:
            config_warning = f"Unable to update Cursor CLI non-Fast config: {exc}"
        # Idempotent — same content produces no disk write. Keeps the
        # global cosmic.md rule in sync with current VM state.
        ensure_cursor_global_instructions(cursor_home=self.config.cursor_home)
        output_path = paths.artifacts / "cursor-last-message.md"
        requested_model = normalize_cursor_model(model)
        command = self.build_command(
            paths=paths,
            prompt=prompt,
            model=requested_model,
            resume_chat_id=resume_chat_id,
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
                requested_model=requested_model,
                resume_session_id=resume_chat_id,
                resume_used=bool(resume_chat_id),
            )

        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(paths.workspace),
            env=self._env(),
            **self._process_group_kwargs(),
        )
        timed_out = False
        cancelled = False
        init_timed_out = False
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        try:
            stdout_parts, stderr_parts, stream_state = await self._communicate_streaming(
                process,
                timeout_sec=timeout_sec or self.config.cursor_timeout_sec,
                event_callback=event_callback,
                cancel_check=cancel_check,
            )
            timed_out = bool(stream_state.get("timed_out"))
            cancelled = bool(stream_state.get("cancelled"))
            init_timed_out = bool(stream_state.get("init_timed_out"))
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
        if config_warning:
            stderr = (config_warning + "\n" + stderr).strip()
        last_message = self._extract_last_message(stdout)
        if last_message:
            output_path.write_text(last_message, encoding="utf-8")
        observed_model = self._extract_observed_model(stdout)
        native_session_id = (
            self._extract_native_session_id(stdout)
            or self._parse_chat_id(stdout)
            or str(resume_chat_id or "").strip()
            or None
        )
        model_mismatch = self._model_mismatch(
            requested_model=requested_model,
            observed_model=observed_model,
        )
        if model_mismatch:
            stderr = (
                stderr.rstrip()
                + "\nCursor initialized a different model than COSMIC requested: "
                + f"requested={requested_model}, observed={observed_model}."
            ).strip()
        return CursorRunResult(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            command=command,
            last_message_path=output_path,
            last_message=last_message,
            duration_sec=duration_sec,
            requested_model=requested_model,
            observed_model=observed_model,
            native_session_id=native_session_id,
            resume_session_id=str(resume_chat_id or "").strip() or None,
            resume_used=bool(str(resume_chat_id or "").strip()),
            model_mismatch=model_mismatch,
            cancelled=cancelled,
            init_timed_out=init_timed_out,
        )

    async def _communicate_streaming(
        self,
        process: asyncio.subprocess.Process,
        *,
        timeout_sec: float,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[list[str], list[str], dict[str, bool]]:
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        state = {
            "cancelled": False,
            "timed_out": False,
            "init_timed_out": False,
            "saw_init": False,
            "saw_output": False,
            "last_output_at": started_at,
            "last_idle_notice_at": started_at,
        }

        async def emit(entry: dict[str, Any]) -> None:
            if event_callback is None:
                return
            try:
                await event_callback(entry)
            except Exception:
                return

        def note_output() -> None:
            now = loop.time()
            state["saw_output"] = True
            state["last_output_at"] = now

        async def read_stdout() -> None:
            if process.stdout is None:
                return
            async for text in iter_stream_lines(
                process.stdout,
                omitted_event_type="cosmic.cursor.large_event_omitted",
            ):
                note_output()
                stdout_parts.append(compact_for_memory(text))
                terminal_event = self._cursor_json_line_to_terminal_event(text, stream="stdout")
                if terminal_event.get("event_type") == "system":
                    state["saw_init"] = True
                if event_callback is not None:
                    await emit(terminal_event)

        async def read_stderr() -> None:
            if process.stderr is None:
                return
            async for text in iter_stream_lines(
                process.stderr,
                omitted_event_type="cosmic.cursor.large_stderr_event_omitted",
            ):
                note_output()
                stderr_parts.append(compact_for_memory(text))
                if event_callback is not None:
                    await emit({
                        "stream": "stderr",
                        "event_type": "stderr",
                        "text": _tail(text.strip(), 2000),
                    })

        readers_done = asyncio.Event()

        async def monitor_process() -> None:
            hard_timeout = max(30.0, float(timeout_sec or self.config.cursor_timeout_sec))
            init_timeout = max(30.0, float(self.config.cursor_init_timeout_sec))
            idle_check = max(30.0, float(self.config.cli_idle_check_sec))
            while process.returncode is None and not readers_done.is_set():
                await asyncio.sleep(min(5.0, idle_check))
                now = loop.time()
                if cancel_check is not None:
                    try:
                        if await cancel_check():
                            state["cancelled"] = True
                            stderr_parts.append("Cursor CLI execution cancelled by COSMIC.\n")
                            await emit({
                                "stream": "system",
                                "event_type": "cursor.cancelled",
                                "text": "Cursor run cancelled by COSMIC.",
                            })
                            await self._terminate_process(process)
                            return
                    except Exception:
                        pass
                if event_callback is not None and not state["saw_init"] and now - started_at >= init_timeout:
                    state["timed_out"] = True
                    state["init_timed_out"] = True
                    stderr_parts.append(
                        f"Cursor Agent did not emit initialization output within {int(init_timeout)} seconds.\n"
                    )
                    await emit({
                        "stream": "system",
                        "event_type": "cursor.init_timeout",
                        "text": f"Cursor Agent did not initialize within {int(init_timeout)} seconds.",
                    })
                    await self._terminate_process(process)
                    return
                if hard_timeout > 0 and now - started_at >= hard_timeout:
                    state["timed_out"] = True
                    stderr_parts.append(
                        f"Cursor CLI exceeded hard execution limit after {int(hard_timeout)} seconds.\n"
                    )
                    await emit({
                        "stream": "system",
                        "event_type": "cursor.timeout",
                        "text": f"Cursor is still running after {int(hard_timeout)} seconds; COSMIC stopped this run.",
                    })
                    await self._terminate_process(process)
                    return
                if state["saw_output"] and now - state["last_output_at"] >= idle_check:
                    if now - state["last_idle_notice_at"] >= idle_check:
                        state["last_idle_notice_at"] = now
                        await emit({
                            "stream": "system",
                            "event_type": "cursor.idle_check",
                            "text": f"Cursor is still running; no CLI output for {int(now - state['last_output_at'])} seconds.",
                            "detail": f"pid={process.pid}; elapsed={int(now - started_at)}s",
                        })

        stdout_task = asyncio.create_task(read_stdout())
        stderr_task = asyncio.create_task(read_stderr())
        monitor_task = asyncio.create_task(monitor_process())
        try:
            await asyncio.gather(stdout_task, stderr_task)
            readers_done.set()
            if process.returncode is None:
                await process.wait()
        finally:
            readers_done.set()
            if not monitor_task.done():
                monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
        await process.wait()
        return stdout_parts, stderr_parts, {
            "cancelled": bool(state["cancelled"]),
            "timed_out": bool(state["timed_out"]),
            "init_timed_out": bool(state["init_timed_out"]),
        }

    async def _terminate_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        self._terminate_process_tree(process)
        try:
            await asyncio.wait_for(process.wait(), timeout=10.0)
        except asyncio.TimeoutError:
            self._kill_process_tree(process)
            await process.wait()

    def _process_group_kwargs(self) -> dict[str, Any]:
        if os.name == "nt":
            return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        return {"start_new_session": True}

    def _terminate_process_tree(self, process: asyncio.subprocess.Process) -> None:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGTERM)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        process.terminate()

    def _kill_process_tree(self, process: asyncio.subprocess.Process) -> None:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except ProcessLookupError:
                return
            except OSError:
                pass
        process.kill()

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

    def _extract_observed_model(self, stdout: str) -> str | None:
        for line in stdout.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "system":
                model = str(payload.get("model") or "").strip()
                if model:
                    return model
        return None

    def _extract_native_session_id(self, stdout: str) -> str | None:
        for line in stdout.splitlines():
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            found = self._find_session_id_in_payload(payload)
            if found:
                return found
        return None

    def _find_session_id_in_payload(self, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key in (
                "chatId",
                "chat_id",
                "sessionId",
                "session_id",
                "conversationId",
                "conversation_id",
                "threadId",
                "thread_id",
            ):
                value = str(payload.get(key) or "").strip()
                if value:
                    return value
            for value in payload.values():
                found = self._find_session_id_in_payload(value)
                if found:
                    return found
        if isinstance(payload, list):
            for item in payload:
                found = self._find_session_id_in_payload(item)
                if found:
                    return found
        return None

    def _parse_chat_id(self, output: str) -> str | None:
        text = str(output or "").strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        found = self._find_session_id_in_payload(payload)
        if found:
            return found
        for pattern in (
            r"\bchat[_-]?id\b\s*[:=]\s*([A-Za-z0-9._:-]{8,})",
            r"\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b",
            r"\b([A-Za-z0-9_-]{16,})\b",
        ):
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    def _model_mismatch(self, *, requested_model: str | None, observed_model: str | None) -> bool:
        requested = str(requested_model or "").strip().lower()
        observed = str(observed_model or "").strip().lower()
        if not requested or not observed:
            return False
        requested_is_fast = "fast" in requested
        observed_is_fast = observed.endswith("fast") or " fast" in observed
        if observed_is_fast and not requested_is_fast:
            return True
        if requested.startswith("composer-2.5"):
            observed_normalized = observed.replace("-", " ")
            return "composer" in observed_normalized and "2.5" not in observed_normalized
        return False

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
            audience="supporting",
        )

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["HOME"] = str(self.config.cursor_home)
        env["CURSOR_AGENT"] = "1"
        existing_path = env.get("PATH", "")
        local_bin = str(Path.home() / ".local" / "bin")
        env["PATH"] = f"{local_bin}{os.pathsep}/usr/local/bin{os.pathsep}{existing_path}"
        return env
