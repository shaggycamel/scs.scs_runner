"""The run envelope: start/end bookends, isolated tasks, update_log rows."""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scs_runner.execute import CommandResult, run_command
from scs_runner.models import Failure, RunResult, Task, TaskResult
from scs_runner.update_log import build_row

logger = logging.getLogger(__name__)

WriteLog = Callable[[dict[str, Any]], None]
Alert = Callable[[list[Failure]], None]


def run(
    tasks: Iterable[Mapping[str, Any] | Task],
    *,
    source: str,
    cadence: str,
    write_log: WriteLog,
    job_kickoff: datetime | None = None,
    alert: Alert | None = None,
    dry_run: bool = False,
    repo_root: str | Path | None = None,
    default_timeout: int | None = None,
    include_paused: bool = False,
) -> RunResult:
    """Execute ``tasks`` in order, logging every outcome to ``write_log``.

    The caller is responsible for selecting the rows (source, cadence, pause,
    ordering). This function only executes what it is given, except that paused
    rows are skipped unless ``include_paused`` is set (used by manual --task
    runs).
    """
    if not source:
        raise ValueError("source is required")
    if not cadence:
        raise ValueError("cadence is required")
    if write_log is None:
        raise ValueError("write_log is required")

    root = Path(repo_root or os.getcwd()).expanduser().resolve()
    kickoff = _aware(job_kickoff)
    result = RunResult(source=source, cadence=cadence, job_kickoff=kickoff)

    if dry_run:
        result.dry_run = True
        for raw in tasks:
            task = _as_task(raw)
            if task is None:
                continue
            if task.pause and not include_paused:
                logger.info("[dry-run] skip paused task: %s", task.task)
                continue
            logger.info("[dry-run] %s: %s", task.task, task.command)
        return result

    log_write_errors: list[str] = []

    def safe_write(row: dict[str, Any]) -> None:
        try:
            write_log(row)
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.exception("could not write update_log row for %s", row.get("task"))
            log_write_errors.append(message)

    safe_write(
        build_row(
            source=source,
            job_kickoff=kickoff,
            cadence=cadence,
            task="process start",
            success=True,
        )
    )

    base_env = dict(os.environ)
    base_env.update(
        {
            "UPDATE_LOG_SOURCE": source,
            "UPDATE_LOG_KICKOFF": kickoff.isoformat(),
            "SCS_CADENCE": cadence,
            "SCS_RUN_SOURCE": source,
        }
    )

    failures: list[Failure] = []
    with tempfile.TemporaryDirectory(prefix="scs-runner-") as temp_dir:
        for index, raw in enumerate(tasks):
            try:
                task = _require_task(raw)
            except Exception as exc:
                name = _raw_task_name(raw)
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("invalid schedule row for %s", name)
                safe_write(
                    build_row(
                        source=source,
                        job_kickoff=kickoff,
                        cadence=cadence,
                        task=name,
                        success=False,
                        error=error,
                    )
                )
                failures.append(Failure(name, error))
                result.results.append(TaskResult(task=name, success=False, error=error))
                continue

            if task.pause and not include_paused:
                logger.info("skip paused task: %s", task.task)
                continue

            timeout = task.timeout_seconds if task.timeout_seconds is not None else default_timeout
            result_file = Path(temp_dir) / f"task-{index}.json"
            logger.info("starting: %s", task.task)
            try:
                command_result = run_command(
                    task,
                    repo_root=root,
                    base_env=base_env,
                    timeout=timeout,
                    result_file=result_file,
                )
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                logger.exception("NOT UPDATED: %s", task.task)
                safe_write(
                    build_row(
                        source=task.source or source,
                        job_kickoff=kickoff,
                        cadence=cadence,
                        task=task.task,
                        success=False,
                        error=error,
                    )
                )
                failures.append(Failure(task.task, error))
                result.results.append(TaskResult(task=task.task, success=False, error=error))
                continue

            if command_result.success:
                safe_write(
                    build_row(
                        source=task.source or source,
                        job_kickoff=kickoff,
                        cadence=cadence,
                        task=task.task,
                        success=True,
                        row_count=command_result.row_count,
                    )
                )
                result.results.append(
                    TaskResult(
                        task=task.task,
                        success=True,
                        row_count=command_result.row_count,
                        duration_seconds=command_result.duration_seconds,
                    )
                )
                logger.info(
                    "finished: %s%s",
                    task.task,
                    f" ({command_result.row_count} rows)" if command_result.row_count is not None else "",
                )
                _log_output(task.task, command_result)
                continue

            error = _command_error(command_result, timeout)
            logger.error("NOT UPDATED: %s: %s", task.task, error)
            safe_write(
                build_row(
                    source=task.source or source,
                    job_kickoff=kickoff,
                    cadence=cadence,
                    task=task.task,
                    success=False,
                    error=error,
                )
            )
            failures.append(Failure(task.task, error))
            result.results.append(
                TaskResult(
                    task=task.task,
                    success=False,
                    error=error,
                    duration_seconds=command_result.duration_seconds,
                )
            )
            _log_output(task.task, command_result)

    safe_write(
        build_row(
            source=source,
            job_kickoff=kickoff,
            cadence=cadence,
            task="process end",
            success=not failures and not log_write_errors,
        )
    )

    if log_write_errors:
        failures.append(
            Failure(
                "util.update_log",
                f"{len(log_write_errors)} log write(s) failed; first error: {log_write_errors[0]}",
            )
        )

    if failures and alert is not None:
        try:
            alert(failures)
        except Exception:
            logger.exception("could not send failure alert")

    result.failures.extend(failures)
    result.log_write_errors.extend(log_write_errors)
    return result


def _as_task(raw: Mapping[str, Any] | Task) -> Task | None:
    try:
        return _require_task(raw)
    except (TypeError, ValueError) as exc:
        logger.error("invalid schedule row %r: %s", raw, exc)
        return None


def _require_task(raw: Mapping[str, Any] | Task) -> Task:
    return raw if isinstance(raw, Task) else Task.from_row(raw)


def _raw_task_name(raw: Mapping[str, Any] | Task) -> str:
    if isinstance(raw, Task):
        return raw.task
    return str(raw.get("task") or "<invalid>")


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _command_error(result: CommandResult, timeout: float | None) -> str:
    if result.timed_out:
        return f"timed out after {timeout}s" if timeout is not None else "timed out"
    message = f"exit code {result.returncode}"
    stderr = result.stderr.strip()
    if stderr:
        message += f": {_tail(stderr)}"
    else:
        stdout = result.stdout.strip()
        if stdout:
            message += f": {_tail(stdout)}"
    return message


def _log_output(task_name: str, result: CommandResult) -> None:
    if result.stdout.strip():
        logger.debug("stdout for %s:\n%s", task_name, result.stdout.rstrip())
    if result.stderr.strip():
        logger.debug("stderr for %s:\n%s", task_name, result.stderr.rstrip())


def _tail(text: str, limit: int = 4000) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]
