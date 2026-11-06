"""Task rows and run results.

The schedule table is deliberately represented as plain mappings at the edge so
Python repos can pass ``hub.db.read(...).to_dicts()`` straight into ``run()``.
``Task`` is the normalised internal form.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "t", "yes", "y"}
    return bool(value)


def _environment(value: Any) -> dict[str, str] | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, Mapping):
        raise TypeError("environment must be a JSON object or mapping")
    env = {str(key): str(val) for key, val in value.items() if val is not None}
    return env or None


@dataclass(frozen=True)
class Task:
    """One row of ``util.update_schedule``."""

    source: str
    task: str
    command: str
    cadence: str | None = None
    sequence: int | None = None
    pause: bool = False
    working_dir: str | None = None
    environment: dict[str, str] | None = None
    timeout_seconds: int | None = None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Task:
        source = _opt_str(row.get("source"))
        task = _opt_str(row.get("task"))
        command = _opt_str(row.get("command"))
        if source is None:
            raise ValueError("schedule row is missing 'source'")
        if task is None:
            raise ValueError("schedule row is missing 'task'")
        if command is None:
            raise ValueError("schedule row is missing 'command'")
        return cls(
            source=source,
            task=task,
            command=command,
            cadence=_opt_str(row.get("cadence")),
            sequence=_opt_int(row.get("sequence")),
            pause=_bool(row.get("pause")),
            working_dir=_opt_str(row.get("working_dir")),
            environment=_environment(row.get("environment")),
            timeout_seconds=_opt_int(row.get("timeout_seconds")),
        )


@dataclass(frozen=True)
class Target:
    """Where a task command runs.

    ``host`` is ``None`` for local execution and a hostname/alias for SSH. A
    local ``root`` is resolved relative to the current directory. A remote
    ``root`` is used as-is on the remote host.
    """

    root: str | Path = "."
    host: str | None = None
    ssh_options: tuple[str, ...] = ()
    environment: Mapping[str, str] = field(default_factory=dict)
    ssh_binary: str = "ssh"

    @property
    def is_remote(self) -> bool:
        return self.host is not None

    @classmethod
    def local(cls, root: str | Path = ".") -> Target:
        return cls(root=root)


@dataclass(frozen=True)
class TaskResult:
    """Outcome of one task."""

    task: str
    success: bool
    error: str | None = None
    row_count: int | None = None
    duration_seconds: float = 0.0
    source: str | None = None


@dataclass(frozen=True)
class Failure:
    """A task failure, or a failure to write util.update_log."""

    task: str
    error: str
    source: str | None = None


@dataclass
class RunResult:
    """Outcome of one run."""

    source: str
    cadence: str
    job_kickoff: datetime
    results: list[TaskResult] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    log_write_errors: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def success(self) -> bool:
        return not self.failures and not self.log_write_errors

    @property
    def exit_code(self) -> int:
        return 0 if self.success else 1
