"""Run one command task and capture its outcome."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scs_runner.models import Task

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    row_count: int | None = None

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def run_command(
    task: Task,
    *,
    repo_root: Path,
    base_env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    result_file: Path | None = None,
) -> CommandResult:
    """Execute ``task.command`` from ``working_dir`` (or the repo root)."""
    cwd = _resolve_cwd(repo_root, task.working_dir)
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("SCS_TASK", task.task)
    env["SCS_TASK_SOURCE"] = task.source
    # Nested log writers in the task should use the owning repo, not the run
    # source (which is the same repo for per-repo invocations).
    env["UPDATE_LOG_SOURCE"] = task.source
    if task.environment:
        env.update(task.environment)
    if result_file is not None:
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.unlink(missing_ok=True)
        env["SCS_RESULT_FILE"] = str(result_file)

    started = time.monotonic()
    try:
        completed = subprocess.run(
            task.command,
            check=False,
            shell=True,
            cwd=cwd,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        stderr = _text(exc.stderr).strip()
        if stderr:
            stderr += "\n"
        stderr += f"command timed out after {timeout}s"
        return CommandResult(
            returncode=124,
            stdout=_text(exc.stdout),
            stderr=stderr,
            duration_seconds=duration,
            timed_out=True,
        )

    duration = time.monotonic() - started
    row_count = None
    if completed.returncode == 0 and result_file is not None:
        row_count = _read_result_file(result_file)
    return CommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        duration_seconds=duration,
        row_count=row_count,
    )


def _resolve_cwd(repo_root: Path, working_dir: str | None) -> Path:
    if not working_dir:
        return repo_root
    path = Path(working_dir)
    return path if path.is_absolute() else repo_root / path


def _read_result_file(path: Path) -> int | None:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not read task result file %s: %s", path, exc)
        return None
    finally:
        path.unlink(missing_ok=True)

    if not isinstance(payload, dict):
        logger.warning("task result file %s must contain a JSON object", path)
        return None
    value = payload.get("row_count")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        logger.warning("task result file %s has a non-integer row_count: %r", path, value)
        return None


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value
