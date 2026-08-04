"""Thin subprocess wrapper for invoking configured EDA commands."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
import subprocess
from typing import Mapping, Sequence

from .reports import parse_measurements


@dataclass(frozen=True)
class EdaCommand:
    command: tuple[str, ...]
    cwd: str | Path | None = None
    timeout_s: float = 120.0
    env: dict[str, str] = field(default_factory=dict)
    measurement_file: str | Path | None = None

    def __init__(self, command: Sequence[str], cwd: str | Path | None = None, timeout_s: float = 120.0, env: Mapping[str, str] | None = None, measurement_file: str | Path | None = None):
        object.__setattr__(self, "command", tuple(str(part) for part in command))
        object.__setattr__(self, "cwd", cwd)
        object.__setattr__(self, "timeout_s", timeout_s)
        object.__setattr__(self, "env", dict(env or {}))
        object.__setattr__(self, "measurement_file", measurement_file)
        if not self.command:
            raise ValueError("EDA command cannot be empty")


@dataclass(frozen=True)
class EdaRunResult:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    metrics: dict[str, float]
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_eda_command(spec: EdaCommand, *, check: bool = False) -> EdaRunResult:
    env = os.environ.copy()
    env.update(spec.env)
    try:
        completed = subprocess.run(
            list(spec.command),
            cwd=spec.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=spec.timeout_s,
            check=False,
        )
        metrics_source: str | Path = completed.stdout + "\n" + completed.stderr
        if spec.measurement_file is not None:
            metric_path = Path(spec.measurement_file)
            if not metric_path.is_absolute() and spec.cwd is not None:
                metric_path = Path(spec.cwd) / metric_path
            metrics_source = metric_path
        result = EdaRunResult(spec.command, completed.returncode, completed.stdout, completed.stderr, parse_measurements(metrics_source), False)
    except subprocess.TimeoutExpired as exc:
        result = EdaRunResult(spec.command, -9, exc.stdout or "", exc.stderr or "", {}, True)
    if check and not result.ok:
        raise RuntimeError(f"EDA command failed rc={result.returncode}: {' '.join(result.command)}")
    return result
