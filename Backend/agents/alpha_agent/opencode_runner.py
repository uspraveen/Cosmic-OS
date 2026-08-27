from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from shared.opencode_cli import find_opencode_binary as resolve_opencode_binary
from shared.opencode_cli import opencode_cli_env
from shared.contracts import ArtifactManifest

from .config import AlphaAgentConfig
from .instructions import ensure_opencode_global_instructions
from .streaming import compact_for_memory, iter_stream_lines
from .workspace_manager import WorkspacePaths


SECRET_PATTERN = re.compile(r"(sk-[^\s\"']{4,}|oc_[^\s\"']{8,})", re.IGNORECASE)
SESSION_KEY_PATTERN = re.compile(
    r"\b(ses|session)[_-]?id[\"':=\s]+([A-Za-z0-9_-]{8,})", re.IGNORECASE
)


def _redact(value: str) -> str:
    return SECRET_PATTERN.sub("<redacted>", value)


def _tail(value: str, limit: int = 8000) -> str:
    redacted = _redact(value or "")
    return redacted[-limit:]


def normalize_opencode_model(value: str | None) -> str | None:
    """Accept bare Zen ids (`mimo-v2.5-free`) or full ids (`opencode/...`).

    Bare ids are Zen ids by convention — every Alpha/OpenCode pairing the
    settings panel writes is prefixed `opencode/` here so the CLI always
    receives a fully-qualified `provider/model`.
    """
    normalized = str(value or "").strip()
    if not normalized or normalized.lower() == "auto":
        return None
    if "/" in normalized:
        return normalized[:160]
    return f"opencode/{normalized}"[:160]


VARIANT_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,40}$")


def normalize_opencode_variant(value: str | None) -> str | None:
    """Reasoning-effort style variants map to `opencode run --variant`."""
    normalized = str(value or "").strip()
    if not normalized or normalized.lower() == "auto":
        return None
    return normalized if VARIANT_PATTERN.match(normalized) else None


@dataclass(frozen=True)
class OpenCodeRunResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    command: list[str]
    last_message_path: Path
    last_message: str
    duration_sec: float
    requested_model: str | None = None
    native_session_id: str | None = None
    resume_session_id: str | None = None
    resume_used: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled

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
            "native_session_id": self.native_session_id,
            "resume_session_id": self.resume_session_id,
            "resume_used": self.resume_used,
            "cancelled": self.cancelled,
        }


class OpenCodeWorkspaceRunner:
    """Runs `opencode run` headlessly inside an Alpha workspace.

    Sessions: opencode creates one per run and reports it in the JSON event
    stream; resume passes `--session <id>`. COSMIC records both directions in
    the project registry (`alpha_harness_sessions`) like it does for Cursor.
    """

    def __init__(self, config: AlphaAgentConfig) -> None:
        self.config = config
        # Optional per-provider API keys ({provider_id: api_key}), set from
        # the gateway's internal status payload. Free Zen models run keyless;
        # an empty map simply means keyless, not broken. A lone legacy Zen
        # key arrives here under the `opencode` id.
        self.provider_keys: dict[str, str] = {}

    @property
    def effective_provider_keys(self) -> dict[str, str]:
        keys = {
            str(pid).strip().lower(): str(key).strip()
            for pid, key in (self.provider_keys or {}).items()
            if str(pid or "").strip() and str(key or "").strip()
        }
        if not keys:
            raw = getattr(self.config, "zen_api_key", "")
            if str(raw or "").strip():
                keys = {"opencode": str(raw).strip()}
        return keys

    def opencode_binary(self) -> str | None:
        return find_opencode_binary_wrapper(self.config.opencode_home)

    def is_available(self) -> bool:
        return self.opencode_binary() is not None

    def build_command(
        self,
        *,
        paths: WorkspacePaths,
        prompt: str,
        model: str | None = None,
        resume_session_id: str | None = None,
        variant: str | None = None,
        json_format: bool = False,
        auto_approve: bool = True,
    ) -> list[str]:
        binary = self.opencode_binary() or "opencode"
        command = [
            binary,
            "run",
            "--dir",
            str(paths.workspace),
        ]
        if auto_approve:
            command.append("--auto")
        normalized_resume = str(resume_session_id or "").strip()
        if normalized_resume:
            command.extend(["--session", normalized_resume])
        normalized_model = normalize_opencode_model(model)
        if normalized_model:
            command.extend(["--model", normalized_model])
        normalized_variant = normalize_opencode_variant(variant)
        if normalized_variant:
            command.extend(["--variant", normalized_variant])
        if json_format:
            command.extend(["--format", "json"])
        command.append(prompt)
        return command

    async def run(
        self,
        *,
        paths: WorkspacePaths,
        prompt: str,
        model: str | None = None,
        resume_session_id: str | None = None,
        variant: str | None = None,
        timeout_sec: float | None = None,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> OpenCodeRunResult:
        paths.workspace.mkdir(parents=True, exist_ok=True)
        paths.artifacts.mkdir(parents=True, exist_ok=True)
        self.config.opencode_home.mkdir(parents=True, exist_ok=True)
        # Idempotent — same content produces no disk write. Keeps the global
        # AGENTS.md under the OpenCode config dir in sync with current VM state.
        try:
            ensure_opencode_global_instructions(opencode_home=self.config.opencode_home)
        except OSError:
            pass
        output_path = paths.artifacts / "opencode-last-message.md"
        requested_model = normalize_opencode_model(model)
        command = self.build_command(
            paths=paths,
            prompt=prompt,
            model=requested_model,
            resume_session_id=resume_session_id,
            variant=variant,
            json_format=event_callback is not None,
        )
        started_at = asyncio.get_running_loop().time()
        if not self.is_available():
            return OpenCodeRunResult(
                returncode=127,
                stdout="",
                stderr="opencode executable not found",
                timed_out=False,
                command=command,
                last_message_path=output_path,
                last_message="",
                duration_sec=0.0,
                requested_model=requested_model,
                resume_session_id=str(resume_session_id or "").strip() or None,
                resume_used=bool(str(resume_session_id or "").strip()),
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
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        try:
            stdout_parts, stderr_parts, stream_state = await self._communicate_streaming(
                process,
                timeout_sec=timeout_sec or self.config.opencode_timeout_sec,
                event_callback=event_callback,
                cancel_check=cancel_check,
            )
            timed_out = bool(stream_state.get("timed_out"))
            cancelled = bool(stream_state.get("cancelled"))
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
        if not last_message and not (event_callback is not None):
            last_message = strip_json_noise(stdout)
        if last_message:
            output_path.write_text(last_message, encoding="utf-8")
        native_session_id = (
            self._extract_native_session_id(stdout)
            or str(resume_session_id or "").strip()
            or None
        )
        return OpenCodeRunResult(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            command=command,
            last_message_path=output_path,
            last_message=last_message,
            duration_sec=duration_sec,
            requested_model=requested_model,
            native_session_id=native_session_id,
            resume_session_id=str(resume_session_id or "").strip() or None,
            resume_used=bool(str(resume_session_id or "").strip()),
            cancelled=cancelled,
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
                omitted_event_type="cosmic.opencode.large_event_omitted",
            ):
                note_output()
                stdout_parts.append(compact_for_memory(text))
                if event_callback is not None:
                    await emit(self._json_line_to_terminal_event(text, stream="stdout"))

        async def read_stderr() -> None:
            if process.stderr is None:
                return
            async for text in iter_stream_lines(
                process.stderr,
                omitted_event_type="cosmic.opencode.large_stderr_event_omitted",
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
            hard_timeout = max(30.0, float(timeout_sec or self.config.opencode_timeout_sec))
            idle_check = max(30.0, float(self.config.cli_idle_check_sec))
            while process.returncode is None and not readers_done.is_set():
                await asyncio.sleep(min(5.0, idle_check))
                now = loop.time()
                if cancel_check is not None:
                    try:
                        if await cancel_check():
                            state["cancelled"] = True
                            stderr_parts.append("OpenCode CLI execution cancelled by COSMIC.\n")
                            await emit({
                                "stream": "system",
                                "event_type": "opencode.cancelled",
                                "text": "OpenCode run cancelled by COSMIC.",
                            })
                            await self._terminate_process(process)
                            return
                    except Exception:
                        pass
                if hard_timeout > 0 and now - started_at >= hard_timeout:
                    state["timed_out"] = True
                    stderr_parts.append(
                        f"OpenCode CLI exceeded hard execution limit after {int(hard_timeout)} seconds.\n"
                    )
                    await emit({
                        "stream": "system",
                        "event_type": "opencode.timeout",
                        "text": f"OpenCode is still running after {int(hard_timeout)} seconds; COSMIC stopped this run.",
                    })
                    await self._terminate_process(process)
                    return
                if state["saw_output"] and now - state["last_output_at"] >= idle_check:
                    if now - state["last_idle_notice_at"] >= idle_check:
                        state["last_idle_notice_at"] = now
                        await emit({
                            "stream": "system",
                            "event_type": "opencode.idle_check",
                            "text": f"OpenCode is still running; no CLI output for {int(now - state['last_output_at'])} seconds.",
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

    def _json_line_to_terminal_event(self, line: str, *, stream: str) -> dict[str, Any]:
        text = line.strip()
        if not text:
            return {"stream": stream, "event_type": "empty", "text": ""}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return {"stream": stream, "event_type": "stdout", "text": _tail(text, 2000)}
        if not isinstance(payload, dict):
            return {"stream": stream, "event_type": "json", "text": _tail(text, 2000)}

        event_type = str(payload.get("type") or "")
        if not event_type and isinstance(payload.get("part"), dict):
            event_type = str(payload["part"].get("type") or "")
        if not event_type:
            event_type = str(payload.get("event") or "opencode.event")
        message = self._extract_event_text(payload)
        detail = self._extract_event_detail(payload)
        return {
            "stream": stream,
            "event_type": event_type,
            "text": _tail(message or event_type, 2000),
            "detail": _tail(detail, 2000) if detail else None,
        }

    def _extract_event_text(self, payload: dict[str, Any]) -> str:
        for key in ("message", "text", "content", "delta", "summary"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        part = payload.get("part")
        if isinstance(part, dict):
            for key in ("text", "content", "command", "summary"):
                value = part.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        error = payload.get("error")
        if isinstance(error, dict):
            name = str(error.get("name") or "").strip()
            message_value = str(error.get("message") or error.get("data") or "").strip()
            combined = " ".join(part for part in (name, message_value) if part).strip()
            if combined:
                return combined[:2000]
        return ""

    def _extract_event_detail(self, payload: dict[str, Any]) -> str | None:
        details: list[str] = []
        for key in ("sessionID", "modelID", "providerID", "cost"):
            value = payload.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                details.append(f"{key}: {value}")
        part = payload.get("part")
        if isinstance(part, dict):
            for key in ("sessionID", "type"):
                value = part.get(key)
                if isinstance(value, str) and value.strip():
                    details.append(f"{key}: {value.strip()}")
        return "; ".join(details)[:400] or None

    def _extract_last_message(self, stdout: str) -> str:
        text_chunks: list[str] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            for key in ("result", "final_message", "lastMessage"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            part = payload.get("part")
            if isinstance(part, dict) and part.get("type") in {"text", "step-start"}:
                value = part.get("text")
                if isinstance(value, str) and value.strip():
                    text_chunks.append(value.strip())
        return "".join(text_chunks).strip()

    def _extract_native_session_id(self, stdout: str) -> str | None:
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                match = SESSION_KEY_PATTERN.search(stripped)
                if match:
                    return match.group(2).strip()
                continue
            found = self._find_session_id_in_payload(payload)
            if found:
                return found
        return None

    def _find_session_id_in_payload(self, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key in (
                "sessionID",
                "session_id",
                "sessionId",
                "id",
            ):
                value = payload.get(key)
                if isinstance(value, str) and value.strip().startswith("ses_"):
                    return value.strip()
            for key in ("sessionID", "session_id", "sessionId"):
                plain = payload.get(key)
                if isinstance(plain, str) and plain.strip():
                    return plain.strip()
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

    def artifact_for_last_message(self, *, task_id: str, result: OpenCodeRunResult) -> ArtifactManifest | None:
        path = result.last_message_path
        if not path.exists() or not result.last_message.strip():
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return ArtifactManifest(
            artifact_id=f"art_alpha_opencode_{digest[:16]}",
            task_id=task_id,
            mime="text/markdown",
            sha256=digest,
            path=str(path),
            created_by_agent="cosmic/alpha-agent:1.0.0",
            kind="output",
            audience="supporting",
        )

    def _env(self) -> dict[str, str]:
        return opencode_cli_env(
            self.config.opencode_home,
            provider_keys=self.effective_provider_keys or None,
        )


def strip_json_noise(stdout: str) -> str:
    """Non-JSON fallback: trailing human-readable lines from `--format default`."""
    lines = [
        line.rstrip()
        for line in stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("{")
    ]
    return "\n".join(lines[-80:]).strip()


def find_opencode_binary_wrapper(opencode_home: Any) -> str | None:
    home = str(opencode_home) if opencode_home else None
    return resolve_opencode_binary(home)
