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

from shared.zcode_cli import (
    ZAI_ANTHROPIC_BASE_URL,
    ensure_zcode_cli_config,
    find_zcode_binary as resolve_zcode_binary,
    normalize_zcode_model,
    normalize_zcode_thinking,
    zcode_cli_env,
)
from shared.contracts import ArtifactManifest

from .config import AlphaAgentConfig
from .instructions import ensure_zcode_global_instructions
from .streaming import compact_for_memory, iter_stream_lines
from .workspace_manager import WorkspacePaths


SECRET_PATTERN = re.compile(r"(sk-[^\s\"']{4,}|eyJ[A-Za-z0-9._-]{16,})", re.IGNORECASE)
SESSION_KEY_PATTERN = re.compile(r"\b(sess[_-]?id)[\"':=\s]+([A-Za-z0-9_-]{8,})", re.IGNORECASE)


def _redact(value: str) -> str:
    return SECRET_PATTERN.sub("<redacted>", value)


def _tail(value: str, limit: int = 8000) -> str:
    redacted = _redact(value or "")
    return redacted[-limit:]


def qualify_zcode_model(value: str | None) -> str | None:
    """Bare COSMIC model id (`glm-5.3-flash`) → CLI ref (`zai/glm-5.3-flash`)."""
    model_id = normalize_zcode_model(value)
    return f"zai/{model_id}" if model_id else None


@dataclass(frozen=True)
class ZcodeRunResult:
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


class ZcodeWorkspaceRunner:
    """Runs `zcode --prompt` headlessly inside an Alpha workspace.

    Streaming: runs use `--output-format stream-json`, which emits one JSON
    event per line (turns, model requests, tool calls) and a final flat
    `{"type": "result", …}` carrying the same shape as the old `--json`
    payload. Mapped events are forwarded through `event_callback` so the
    Alpha card shows live progress instead of silence; older CLIs without
    stream-json get one legacy `--json` retry when the stream attempt dies
    without a single event.

    Sessions: ZCode persists one per run and reports it as `sessionId` in
    the result event; resume passes `--resume <sess_…>`. COSMIC records both
    directions in the project registry (`alpha_harness_sessions`) like it
    does for Cursor and OpenCode.

    Auth and the thinking default live in the ZCode home's
    `.zcode/cli/config.json` (the gateway and `zcode login` own that file);
    this runner only overrides the model per run via `ZCODE_MODEL`.
    """

    def __init__(self, config: AlphaAgentConfig) -> None:
        self.config = config

    def zcode_binary(self) -> str | None:
        return find_zcode_binary_wrapper(self.config.zcode_home)

    def is_available(self) -> bool:
        return self.zcode_binary() is not None

    def build_command(
        self,
        *,
        paths: WorkspacePaths,
        prompt: str,
        model: str | None = None,
        resume_session_id: str | None = None,
        stream_format: bool = True,
    ) -> list[str]:
        binary = self.zcode_binary() or "zcode"
        command = [
            binary,
            "--prompt",
            prompt,
            "--mode",
            "yolo",
            "--cwd",
            str(paths.workspace),
            "--no-color",
        ]
        normalized_resume = str(resume_session_id or "").strip()
        if normalized_resume:
            command.extend(["--resume", normalized_resume])
        if stream_format:
            command.extend(["--output-format", "stream-json"])
        else:
            command.append("--json")
        return command

    async def run(
        self,
        *,
        paths: WorkspacePaths,
        prompt: str,
        model: str | None = None,
        thinking: str | None = None,
        resume_session_id: str | None = None,
        timeout_sec: float | None = None,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> ZcodeRunResult:
        paths.workspace.mkdir(parents=True, exist_ok=True)
        paths.artifacts.mkdir(parents=True, exist_ok=True)
        self.config.zcode_home.mkdir(parents=True, exist_ok=True)
        # Idempotent — same content produces no disk write. Keeps the global
        # ~/.zcode/AGENTS.md under the ZCode home in sync with current VM state.
        try:
            ensure_zcode_global_instructions(zcode_home=self.config.zcode_home)
        except OSError:
            pass
        # The thinking default is config-driven (the CLI has no per-run flag);
        # ensure it here so a task-level pick applies even before any gateway
        # save. Merges around the stored API key — never touches auth.
        if thinking:
            try:
                ensure_zcode_cli_config(
                    self.config.zcode_home,
                    thinking=thinking,
                )
            except OSError:
                pass
        output_path = paths.artifacts / "zcode-last-message.md"
        requested_model = normalize_zcode_model(model)
        command = self.build_command(
            paths=paths,
            prompt=prompt,
            model=requested_model,
            resume_session_id=resume_session_id,
            stream_format=True,
        )
        started_at = asyncio.get_running_loop().time()
        if not self.is_available():
            return ZcodeRunResult(
                returncode=127,
                stdout="",
                stderr="zcode executable not found",
                timed_out=False,
                command=command,
                last_message_path=output_path,
                last_message="",
                duration_sec=0.0,
                requested_model=requested_model,
                resume_session_id=str(resume_session_id or "").strip() or None,
                resume_used=bool(str(resume_session_id or "").strip()),
            )

        outcome = await self._spawn_and_communicate(
            command,
            paths=paths,
            requested_model=requested_model,
            timeout_sec=timeout_sec,
            event_callback=event_callback,
            cancel_check=cancel_check,
        )
        # Legacy CLIs without stream-json die on the unknown flag with an
        # empty stream; retry once in the old whole-payload `--json` mode so
        # a stale CLI degrades to silent-but-working instead of broken.
        if (
            outcome["final_payload"] is None
            and outcome["stream_event_count"] == 0
            and outcome["process"].returncode not in (0, None)
        ):
            await outcome["emit"]({
                "stream": "system",
                "event_type": "zcode.stream_fallback",
                "text": "ZCode CLI did not answer in stream-json mode; retrying once in legacy mode.",
                "detail": f"returncode={outcome['process'].returncode}; stderr_tail={_tail(outcome['stderr'], 300)}",
            })
            legacy_command = self.build_command(
                paths=paths,
                prompt=prompt,
                model=requested_model,
                resume_session_id=resume_session_id,
                stream_format=False,
            )
            outcome = await self._spawn_and_communicate(
                legacy_command,
                paths=paths,
                requested_model=requested_model,
                timeout_sec=timeout_sec,
                event_callback=event_callback,
                cancel_check=cancel_check,
            )
            command = legacy_command

        process = outcome["process"]
        duration_sec = asyncio.get_running_loop().time() - started_at
        stdout = "".join(outcome["stdout_parts"])
        stderr = "".join(outcome["stderr_parts"])
        payload = outcome["final_payload"] or self._extract_json_payload(stdout)
        if payload is not None:
            last_message = str(payload.get("response") or "").strip()
        else:
            last_message = strip_json_noise(stdout)
        if last_message:
            output_path.write_text(last_message, encoding="utf-8")
        native_session_id = (
            self._extract_native_session_id(stdout, payload)
            or str(resume_session_id or "").strip()
            or None
        )
        return ZcodeRunResult(
            returncode=process.returncode,
            stdout=stdout,
            stderr=stderr,
            timed_out=outcome["timed_out"],
            command=command,
            last_message_path=output_path,
            last_message=last_message,
            duration_sec=duration_sec,
            requested_model=requested_model,
            native_session_id=native_session_id,
            resume_session_id=str(resume_session_id or "").strip() or None,
            resume_used=bool(str(resume_session_id or "").strip()),
            cancelled=outcome["cancelled"],
        )

    async def _spawn_and_communicate(
        self,
        command: list[str],
        *,
        paths: WorkspacePaths,
        requested_model: str | None,
        timeout_sec: float | None,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None,
        cancel_check: Callable[[], Awaitable[bool]] | None,
    ) -> dict[str, Any]:
        """Spawn one CLI attempt and drain its stream.

        Returns the process, raw stdout/stderr parts, the stream-json final
        `result` payload when seen, a count of mapped stream events, and the
        cancel/timeout state — everything `run` needs to assemble the result
        or decide on a legacy retry.
        """
        process = await asyncio.create_subprocess_exec(
            *command,
            stdin=subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(paths.workspace),
            env=self._env(requested_model),
            **self._process_group_kwargs(),
        )
        timed_out = False
        cancelled = False
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        try:
            stdout_parts, stderr_parts, stream_state, final_payload, event_count = (
                await self._communicate_streaming(
                    process,
                    timeout_sec=timeout_sec or self.config.zcode_timeout_sec,
                    event_callback=event_callback,
                    cancel_check=cancel_check,
                )
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

        async def emit(entry: dict[str, Any]) -> None:
            if event_callback is None:
                return
            try:
                await event_callback(entry)
            except Exception:
                return

        return {
            "process": process,
            "stdout_parts": stdout_parts,
            "stderr_parts": stderr_parts,
            "timed_out": timed_out,
            "cancelled": cancelled,
            "final_payload": final_payload,
            "stream_event_count": event_count,
            "stderr": "".join(stderr_parts),
            "emit": emit,
        }

    async def _communicate_streaming(
        self,
        process: asyncio.subprocess.Process,
        *,
        timeout_sec: float,
        event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        cancel_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> tuple[list[str], list[str], dict[str, bool], dict[str, Any] | None, int]:
        stdout_parts: list[str] = []
        stderr_parts: list[str] = []
        final_payload: dict[str, Any] | None = None
        stream_event_count = 0
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
            nonlocal final_payload, stream_event_count
            if process.stdout is None:
                return
            async for text in iter_stream_lines(
                process.stdout,
                omitted_event_type="cosmic.zcode.large_event_omitted",
            ):
                note_output()
                stdout_parts.append(compact_for_memory(text))
                stripped = text.strip()
                parsed: Any = None
                if stripped.startswith("{"):
                    try:
                        parsed = json.loads(stripped)
                    except json.JSONDecodeError:
                        parsed = None
                if isinstance(parsed, dict):
                    if parsed.get("type") == "result":
                        # The final flat payload — same shape as `--json`.
                        # Stash the parsed object (not the possibly-compacted
                        # line) so a long response survives memory trimming.
                        final_payload = parsed
                        continue
                    payload = parsed.get("payload")
                    if isinstance(payload, dict) and payload.get("type"):
                        stream_event_count += 1
                        progress_line = _progress_line_for_event(payload)
                        if progress_line is not None:
                            await emit({
                                "stream": "stdout",
                                "event_type": f"zcode.{payload.get('type')}",
                                "text": _tail(progress_line, 2000),
                            })
                        continue
                if stripped:
                    await emit({
                        "stream": "stdout",
                        "event_type": "stdout",
                        "text": _tail(stripped, 500),
                    })

        async def read_stderr() -> None:
            if process.stderr is None:
                return
            async for text in iter_stream_lines(
                process.stderr,
                omitted_event_type="cosmic.zcode.large_stderr_event_omitted",
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
            hard_timeout = max(30.0, float(timeout_sec or self.config.zcode_timeout_sec))
            idle_check = max(30.0, float(self.config.cli_idle_check_sec))
            while process.returncode is None and not readers_done.is_set():
                await asyncio.sleep(min(5.0, idle_check))
                now = loop.time()
                if cancel_check is not None:
                    try:
                        if await cancel_check():
                            state["cancelled"] = True
                            stderr_parts.append("ZCode CLI execution cancelled by COSMIC.\n")
                            await emit({
                                "stream": "system",
                                "event_type": "zcode.cancelled",
                                "text": "ZCode run cancelled by COSMIC.",
                            })
                            await self._terminate_process(process)
                            return
                    except Exception:
                        pass
                if hard_timeout > 0 and now - started_at >= hard_timeout:
                    state["timed_out"] = True
                    stderr_parts.append(
                        f"ZCode CLI exceeded hard execution limit after {int(hard_timeout)} seconds.\n"
                    )
                    await emit({
                        "stream": "system",
                        "event_type": "zcode.timeout",
                        "text": f"ZCode is still running after {int(hard_timeout)} seconds; COSMIC stopped this run.",
                    })
                    await self._terminate_process(process)
                    return
                if state["saw_output"] and now - state["last_output_at"] >= idle_check:
                    if now - state["last_idle_notice_at"] >= idle_check:
                        state["last_idle_notice_at"] = now
                        await emit({
                            "stream": "system",
                            "event_type": "zcode.idle_check",
                            "text": f"ZCode is still running; no CLI output for {int(now - state['last_output_at'])} seconds.",
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
        }, final_payload, stream_event_count

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

    @staticmethod
    def _extract_json_payload(stdout: str) -> dict[str, Any] | None:
        """The `--json` run summary is one JSON object on stdout."""
        text = (stdout or "").strip()
        if not text:
            return None
        try:
            payload = json.loads(text)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            for line in reversed(text.splitlines()):
                stripped = line.strip()
                if not stripped.startswith("{"):
                    continue
                try:
                    payload = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(payload, dict) and ("sessionId" in payload or "response" in payload):
                    return payload
            return None

    def _extract_native_session_id(
        self,
        stdout: str,
        payload: dict[str, Any] | None,
    ) -> str | None:
        if payload is not None:
            session_id = str(payload.get("sessionId") or "").strip()
            if session_id:
                return session_id
        for line in stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                parsed = json.loads(stripped)
            except json.JSONDecodeError:
                match = SESSION_KEY_PATTERN.search(stripped)
                if match:
                    return match.group(2).strip()
                continue
            found = self._find_session_id_in_payload(parsed)
            if found:
                return found
        return None

    def _find_session_id_in_payload(self, payload: Any) -> str | None:
        if isinstance(payload, dict):
            for key in ("sessionId", "session_id", "sess_id"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip().startswith("sess_"):
                    return value.strip()
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

    def artifact_for_last_message(self, *, task_id: str, result: ZcodeRunResult) -> ArtifactManifest | None:
        path = result.last_message_path
        if not path.exists() or not result.last_message.strip():
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        return ArtifactManifest(
            artifact_id=f"art_alpha_zcode_{digest[:16]}",
            task_id=task_id,
            mime="text/markdown",
            sha256=digest,
            path=str(path),
            created_by_agent="cosmic/alpha-agent:1.0.0",
            kind="output",
            audience="supporting",
        )

    def _env(self, model: str | None) -> dict[str, str]:
        return zcode_cli_env(
            self.config.zcode_home,
            model=model,
            base_url=ZAI_ANTHROPIC_BASE_URL,
        )


def strip_json_noise(stdout: str) -> str:
    """Non-JSON fallback: trailing human-readable lines from plain runs."""
    lines = [
        line.rstrip()
        for line in stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("{")
    ]
    return "\n".join(lines[-80:]).strip()


_PROGRESS_TOOL_TARGET_KEYS = ("file_path", "command", "url", "path", "query", "pattern")


def _tool_target_label(tool_input: Any) -> str:
    """A short human target for a tool call input (`Write app.py`)."""
    if not isinstance(tool_input, dict):
        return ""
    for key in _PROGRESS_TOOL_TARGET_KEYS:
        value = tool_input.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()[:80]
    return ""


def _progress_line_for_event(payload: dict[str, Any]) -> str | None:
    """Map one stream-json payload to a concise terminal line.

    Deltas, checkpoints, session bookkeeping, and recovery noise are skipped —
    the card wants turn/request/tool rhythm, not hundreds of token fragments.
    """
    event_type = str(payload.get("type") or "")
    if event_type == "turn.started":
        turn_number = int(payload.get("turnNumber") or 0) + 1
        return f"── turn {turn_number} started ──"
    if event_type == "model_request_started":
        model_ref = payload.get("model")
        model_id = ""
        if isinstance(model_ref, dict):
            model_id = str(model_ref.get("modelId") or "")
        return f"▲ model request {model_id}".rstrip()
    if event_type == "model_request_completed":
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        bits = [
            f"in {usage.get('inputTokens', '?')} tok",
            f"out {usage.get('outputTokens', '?')} tok",
        ]
        duration_ms = payload.get("durationMs")
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            bits.append(f"{duration_ms / 1000:.1f}s")
        return "● model request done · " + " · ".join(bits)
    if event_type == "tool.updated":
        tool_name = str(payload.get("toolName") or "tool")
        if str(payload.get("kind") or "") == "tool_input_start":
            target = _tool_target_label(payload.get("input"))
            return f"→ {tool_name}{(' ' + target) if target else ''}"
        if str(payload.get("status") or "") == "tool_result_committed":
            return f"✓ {tool_name}"
        return None
    if event_type == "turn.completed":
        result_type = str(payload.get("resultType") or "success")
        if result_type not in ("", "success"):
            return f"⚠ turn ended: {result_type}"
        bits = []
        token_count = payload.get("tokenCount")
        if isinstance(token_count, (int, float)) and token_count > 0:
            bits.append(f"{int(token_count)} tok")
        duration_ms = payload.get("duration")
        if isinstance(duration_ms, (int, float)) and duration_ms > 0:
            bits.append(f"{duration_ms / 1000:.1f}s")
        tool_calls = payload.get("toolCallCount")
        if isinstance(tool_calls, (int, float)) and tool_calls > 0:
            bits.append(f"{int(tool_calls)} tools")
        summary = f"── turn completed · {' · '.join(bits)}" if bits else "── turn completed"
        response = str(payload.get("response") or "").strip()
        if response:
            summary += f" — {response[:240]}"
        return summary
    return None


def find_zcode_binary_wrapper(zcode_home: Any) -> str | None:
    home = str(zcode_home) if zcode_home else None
    return resolve_zcode_binary(home)
