from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from .config import AlphaAgentConfig
from .workspace_manager import WorkspacePaths


@dataclass(frozen=True)
class DockerRunResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout[-4000:],
            "stderr": self.stderr[-4000:],
            "timed_out": self.timed_out,
        }


class DockerWorkspaceRunner:
    def __init__(self, config: AlphaAgentConfig) -> None:
        self.config = config

    def docker_binary(self) -> str | None:
        return shutil.which("docker")

    def is_available(self) -> bool:
        return self.docker_binary() is not None

    def build_command(
        self,
        *,
        paths: WorkspacePaths,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
    ) -> list[str]:
        docker = self.docker_binary() or "docker"
        docker_command = [
            docker,
            "run",
            "--rm",
            "--workdir",
            "/workspace",
            "--network",
            self.config.docker_network,
            "--memory",
            self.config.docker_memory,
            "--cpus",
            self.config.docker_cpus,
            "--pids-limit",
            str(self.config.docker_pids_limit),
            "--volume",
            f"{self._mount_path(paths.workspace)}:/workspace",
            "--volume",
            f"{self._mount_path(paths.artifacts)}:/artifacts",
            "--volume",
            f"{self._mount_path(paths.codex_home)}:/codex-home",
            "--env",
            "HOME=/codex-home",
            "--env",
            "CODEX_HOME=/codex-home",
            "--env",
            "COSMIC_ALPHA_WORKSPACE=/workspace",
            "--env",
            "COSMIC_ALPHA_ARTIFACTS=/artifacts",
        ]
        for key, value in sorted((env or {}).items()):
            if key and value is not None:
                docker_command.extend(["--env", f"{key}={value}"])
        docker_command.append(self.config.docker_image)
        docker_command.extend(str(part) for part in command)
        return docker_command

    async def run(
        self,
        *,
        paths: WorkspacePaths,
        command: Sequence[str],
        env: Mapping[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> DockerRunResult:
        if not self.is_available():
            return DockerRunResult(
                returncode=127,
                stdout="",
                stderr="docker executable not found",
            )
        argv = self.build_command(paths=paths, command=command, env=env)
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_sec or self.config.docker_timeout_sec,
            )
        except asyncio.TimeoutError:
            process.kill()
            stdout_bytes, stderr_bytes = await process.communicate()
            return DockerRunResult(
                returncode=process.returncode if process.returncode is not None else -9,
                stdout=stdout_bytes.decode("utf-8", errors="replace"),
                stderr=stderr_bytes.decode("utf-8", errors="replace"),
                timed_out=True,
            )
        return DockerRunResult(
            returncode=process.returncode if process.returncode is not None else 0,
            stdout=stdout_bytes.decode("utf-8", errors="replace"),
            stderr=stderr_bytes.decode("utf-8", errors="replace"),
        )

    def _mount_path(self, path: Path) -> str:
        return str(path.resolve())

