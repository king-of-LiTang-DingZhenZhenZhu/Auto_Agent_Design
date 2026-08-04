"""Execution-target abstraction for local and SSH-hosted EDA tools."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
import re
import shlex
import shutil
from typing import Iterable, Mapping
from urllib.parse import urlparse

from .command import EdaCommand, EdaRunResult, run_eda_command


class ExecutionTargetKind(str, Enum):
    LOCAL = "local"
    SSH = "ssh"


@dataclass(frozen=True)
class TargetProbe:
    kind: str
    requested: str
    available: bool
    resolved: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ExecutionTargetPreflight:
    target: Mapping[str, object]
    probes: tuple[TargetProbe, ...]

    @property
    def ready(self) -> bool:
        return all(item.available for item in self.probes)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "analogskills.execution_target_preflight/v1",
            "ready": self.ready,
            "target": dict(self.target),
            "probes": [item.to_dict() for item in self.probes],
        }


@dataclass(frozen=True)
class EdaExecutionTarget:
    kind: ExecutionTargetKind = ExecutionTargetKind.LOCAL
    host: str = ""
    user: str = ""
    port: int = 22
    identity_file: str = ""
    connect_timeout_s: int = 8
    ssh_binary: str = "ssh"
    metadata: Mapping[str, object] = field(default_factory=dict)

    @classmethod
    def parse(cls, value: str | None) -> "EdaExecutionTarget":
        raw = str(value or "local").strip()
        if raw.lower() in {"", "local", "localhost"}:
            return cls()
        parsed = urlparse(raw if "://" in raw else f"ssh://{raw}")
        if parsed.scheme.lower() != "ssh" or not parsed.hostname:
            raise ValueError(f"unsupported EDA execution target: {value!r}")
        return cls(
            ExecutionTargetKind.SSH,
            host=parsed.hostname,
            user=parsed.username or "",
            port=parsed.port or 22,
        )

    @property
    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "host": self.host,
            "user": self.user,
            "port": self.port,
            "identity_file": self.identity_file,
            "connect_timeout_s": self.connect_timeout_s,
            "metadata": dict(self.metadata),
        }

    def wrap_command(self, command: EdaCommand) -> EdaCommand:
        if self.kind is ExecutionTargetKind.LOCAL:
            return command
        remote = _remote_shell_command(command)
        argv = [
            self.ssh_binary,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={max(1, int(self.connect_timeout_s))}",
            "-p",
            str(self.port),
        ]
        if self.identity_file:
            argv.extend(("-i", self.identity_file))
        argv.extend((self.destination, remote))
        return EdaCommand(argv, timeout_s=command.timeout_s)

    def run(self, command: EdaCommand, *, check: bool = False) -> EdaRunResult:
        return run_eda_command(self.wrap_command(command), check=check)

    def resolve_executable(self, executable: str) -> str:
        requested = str(executable)
        if self.kind is ExecutionTargetKind.LOCAL:
            return shutil.which(requested) or ""
        result = self.run(EdaCommand(("command", "-v", requested), timeout_s=max(5, self.connect_timeout_s + 2)))
        return result.stdout.strip().splitlines()[-1] if result.ok and result.stdout.strip() else ""

    def path_readable(self, path: str | Path) -> bool:
        requested = str(path)
        if self.kind is ExecutionTargetKind.LOCAL:
            return Path(requested).is_file()
        return self.run(EdaCommand(("test", "-r", requested), timeout_s=max(5, self.connect_timeout_s + 2))).ok

    def preflight(
        self,
        *,
        executables: Iterable[str] = (),
        readable_paths: Iterable[str | Path] = (),
    ) -> ExecutionTargetPreflight:
        probes: list[TargetProbe] = []
        for executable in executables:
            resolved = self.resolve_executable(str(executable))
            probes.append(TargetProbe("executable", str(executable), bool(resolved), resolved))
        for path in readable_paths:
            available = self.path_readable(path)
            probes.append(TargetProbe("readable_path", str(path), available, str(path) if available else ""))
        return ExecutionTargetPreflight(self.to_dict(), tuple(probes))

    def upload(self, local_path: str | Path, remote_path: str | Path, *, recursive: bool = False) -> EdaRunResult:
        if self.kind is ExecutionTargetKind.LOCAL:
            raise ValueError("upload is only valid for an SSH execution target")
        source = Path(local_path)
        if not source.exists():
            raise FileNotFoundError(source)
        command = [self._scp_binary()]
        if recursive:
            command.append("-r")
        command.extend(self._scp_options())
        command.extend((str(source), f"{self.destination}:{str(remote_path)}"))
        return run_eda_command(EdaCommand(command, timeout_s=120.0))

    def download(self, remote_path: str, local_path: str | Path, *, recursive: bool = False) -> EdaRunResult:
        if self.kind is ExecutionTargetKind.LOCAL:
            raise ValueError("download is only valid for an SSH execution target")
        target = Path(local_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        command = [self._scp_binary()]
        if recursive:
            command.append("-r")
        command.extend(self._scp_options())
        command.extend((f"{self.destination}:{remote_path}", str(target)))
        return run_eda_command(EdaCommand(command, timeout_s=120.0))

    def _scp_options(self) -> list[str]:
        options = ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={max(1, int(self.connect_timeout_s))}", "-P", str(self.port)]
        if self.identity_file:
            options.extend(("-i", self.identity_file))
        return options

    def _scp_binary(self) -> str:
        return str(Path(self.ssh_binary).with_name("scp")) if Path(self.ssh_binary).name.lower().startswith("ssh") else "scp"


def _remote_shell_command(command: EdaCommand) -> str:
    if not command.command:
        raise ValueError("remote EDA command cannot be empty")
    pieces: list[str] = []
    if command.cwd is not None:
        pieces.append(f"cd -- {shlex.quote(str(command.cwd))}")
    env_parts: list[str] = []
    for key, value in command.env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(key)):
            raise ValueError(f"invalid environment variable name: {key!r}")
        env_parts.append(f"{key}={shlex.quote(str(value))}")
    argv = " ".join(shlex.quote(str(part)) for part in command.command)
    invocation = f"env {' '.join(env_parts)} {argv}" if env_parts else argv
    pieces.append(invocation)
    return " && ".join(pieces)
