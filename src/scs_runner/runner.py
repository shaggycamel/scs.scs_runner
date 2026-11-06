"""The run envelope: start/end bookends, isolated tasks, update_log rows."""

from __future__ import annotations

import logging
import os
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from scs_runner.execute import CommandResult, run_command
from scs_runner.models import Failure, RunResult, Target, Task, TaskResult
from scs_runner.update_log import build_row

logger = logging.getLogger(__name__)

WriteLog = Callable[[dict[str, Any]], None]
Alert = Callable[[list[Failure]], None]
Bookends = Literal["source", "run", "none"]


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
    target: Target | None = None,
    default_timeout: int | None = None,
    include_paused: bool = False,
) -> RunResult:
    """Execute ``tasks`` in order, logging every outcome to ``write_log``.

    This is the single-source form. The caller is responsible for selecting the
    rows (source, cadence, pause, ordering). Paused rows are skipped unless
    ``include_paused`` is set (used by manual --task runs).
    """
    if not source:
        raise ValueError("source is required")
    if not cadence:
        raise ValueError("cadence is required")
    if write_log is None:
        raise ValueError("write_log is required")

    kickoff = _aware(job_kickoff)
    result = RunResult(source=source, cadence=cadence, job_kickoff=kickoff)
    rows = list(tasks)

    if target is None:
        target = Target.local(repo_root or os.getcwd())

    if dry_run:
        result.dry_run = True
        for raw in rows:
            task = _as_task(raw)
            if task is None or (task.pause and not include_paused):
                continue
            logger.info("[dry-run] %s: %s", task.task, task.command)
        return result

    log_write_errors: list[str] = []

    def safe_write(row: dict[str, Any]) -> None:
        _safe_write(row, write_log, log_write_errors)

    safe_write(
        build_row(
            source=source,
            job_kickoff=kickoff,
            cadence=cadence,
            task="process start",
            success=True,
        )
    )

    failures: list[Failure] = []
    base_env = _base_env(source, cadence, kickoff)
    with tempfile.TemporaryDirectory(prefix="scs-runner-") as temp_dir:
        _run_group(
            rows,
            default_source=source,
            cadence=cadence,
            safe_write=safe_write,
            target_for=lambda _source: target,
            job_kickoff=kickoff,
            base_env=base_env,
            default_timeout=default_timeout,
            include_paused=include_paused,
            temp_dir=Path(temp_dir),
            failures=failures,
            results=result.results,
        )

    safe_write(
        build_row(
            source=source,
            job_kickoff=kickoff,
            cadence=cadence,
            task="process end",
            success=not failures and not log_write_errors,
        )
    )

    _finish(result, failures, log_write_errors, alert)
    return result


def run_sources(
    tasks: Iterable[Mapping[str, Any] | Task],
    *,
    source: str,
    cadence: str,
    write_log: WriteLog,
    targets: Mapping[str, Target],
    order: Sequence[str] | None = None,
    job_kickoff: datetime | None = None,
    alert: Alert | None = None,
    dry_run: bool = False,
    default_timeout: int | None = None,
    include_paused: bool = False,
    bookends: Bookends = "source",
    repo_root: str | Path | None = None,
) -> RunResult:
    """Execute tasks from one or more sources, each at its own target.

    ``source`` labels the run in alerts and, with ``bookends="run"``, in the
    process start/end rows. With the default ``bookends="source"``, each source
    gets its own process start/end rows, which keeps ``util.update_log`` rows
    attributable to the repo that owns them.
    """
    if not source:
        raise ValueError("source is required")
    if not cadence:
        raise ValueError("cadence is required")
    if write_log is None:
        raise ValueError("write_log is required")
    if bookends not in {"source", "run", "none"}:
        raise ValueError("bookends must be 'source', 'run' or 'none'")

    kickoff = _aware(job_kickoff)
    result = RunResult(source=source, cadence=cadence, job_kickoff=kickoff)
    rows = list(tasks)
    groups, invalid_rows = _group_tasks(rows, order)

    if repo_root is not None:
        local_target = Target.local(repo_root)
        targets = {name: local_target for name in groups} | dict(targets)

    if dry_run:
        result.dry_run = True
        for name in groups:
            target = targets.get(name)
            for task in groups[name]:
                if task.pause and not include_paused:
                    continue
                logger.info(
                    "[dry-run] %s -> %s %s: %s",
                    task.source,
                    _target_label(target),
                    task.task,
                    task.command,
                )
        for raw in invalid_rows:
            logger.info("[dry-run] invalid schedule row: %r", raw)
        return result

    log_write_errors: list[str] = []

    def safe_write(row: dict[str, Any]) -> None:
        _safe_write(row, write_log, log_write_errors)

    failures: list[Failure] = []
    base_env = _base_env(source, cadence, kickoff)

    if bookends == "run":
        safe_write(
            build_row(
                source=source,
                job_kickoff=kickoff,
                cadence=cadence,
                task="process start",
                success=True,
            )
        )

    with tempfile.TemporaryDirectory(prefix="scs-runner-") as temp_dir:
        temp_path = Path(temp_dir)
        for name in groups:
            before_failures = len(failures)
            before_log_errors = len(log_write_errors)
            if bookends == "source":
                safe_write(
                    build_row(
                        source=name,
                        job_kickoff=kickoff,
                        cadence=cadence,
                        task="process start",
                        success=True,
                    )
                )

            _run_group(
                groups[name],
                default_source=name,
                cadence=cadence,
                safe_write=safe_write,
                target_for=lambda source_name: _require_target(targets, source_name),
                job_kickoff=kickoff,
                base_env=base_env,
                default_timeout=default_timeout,
                include_paused=include_paused,
                temp_dir=temp_path,
                failures=failures,
                results=result.results,
            )

            if bookends == "source":
                safe_write(
                    build_row(
                        source=name,
                        job_kickoff=kickoff,
                        cadence=cadence,
                        task="process end",
                        success=(
                            len(failures) == before_failures
                            and len(log_write_errors) == before_log_errors
                        ),
                    )
                )

        if invalid_rows:
            _run_group(
                invalid_rows,
                default_source=source,
                cadence=cadence,
                safe_write=safe_write,
                target_for=lambda source_name: _require_target(targets, source_name),
                job_kickoff=kickoff,
                base_env=base_env,
                default_timeout=default_timeout,
                include_paused=include_paused,
                temp_dir=temp_path,
                failures=failures,
                results=result.results,
            )

    if bookends == "run":
        safe_write(
            build_row(
                source=source,
                job_kickoff=kickoff,
                cadence=cadence,
                task="process end",
                success=not failures and not log_write_errors,
            )
        )

    _finish(result, failures, log_write_errors, alert)
    return result


def _run_group(
    raw_tasks: Sequence[Mapping[str, Any] | Task],
    *,
    default_source: str,
    cadence: str,
    safe_write: Callable[[dict[str, Any]], None],
    target_for: Callable[[str], Target],
    job_kickoff: datetime,
    base_env: Mapping[str, str],
    default_timeout: int | None,
    include_paused: bool,
    temp_dir: Path,
    failures: list[Failure],
    results: list[TaskResult],
) -> None:
    for raw in raw_tasks:
        try:
            task = _require_task(raw)
        except Exception as exc:
            name = _raw_task_name(raw)
            source = _raw_task_source(raw) or default_source
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("invalid schedule row for %s", name)
            safe_write(
                build_row(
                    source=source,
                    job_kickoff=job_kickoff,
                    cadence=cadence,
                    task=name,
                    success=False,
                    error=error,
                )
            )
            failures.append(Failure(name, error, source=source))
            results.append(
                TaskResult(task=name, success=False, error=error, source=source)
            )
            continue

        if task.pause and not include_paused:
            logger.info("skip paused task: %s", task.task)
            continue

        timeout = task.timeout_seconds if task.timeout_seconds is not None else default_timeout
        result_file = temp_dir / f"task-{uuid.uuid4().hex}.json"
        logger.info("starting: %s", task.task)
        try:
            command_result = run_command(
                task,
                target=target_for(task.source),
                base_env=base_env,
                timeout=timeout,
                result_file=result_file,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("NOT UPDATED: %s", task.task)
            safe_write(
                build_row(
                    source=task.source,
                    job_kickoff=job_kickoff,
                    cadence=cadence,
                    task=task.task,
                    success=False,
                    error=error,
                )
            )
            failures.append(Failure(task.task, error, source=task.source))
            results.append(
                TaskResult(task=task.task, success=False, error=error, source=task.source)
            )
            continue

        if command_result.success:
            safe_write(
                build_row(
                    source=task.source,
                    job_kickoff=job_kickoff,
                    cadence=cadence,
                    task=task.task,
                    success=True,
                    row_count=command_result.row_count,
                )
            )
            results.append(
                TaskResult(
                    task=task.task,
                    success=True,
                    row_count=command_result.row_count,
                    duration_seconds=command_result.duration_seconds,
                    source=task.source,
                )
            )
            logger.info(
                "finished: %s%s",
                task.task,
                f" ({command_result.row_count} rows)"
                if command_result.row_count is not None
                else "",
            )
            continue

        error = _command_error(command_result, timeout)
        logger.error("NOT UPDATED: %s: %s", task.task, error)
        safe_write(
            build_row(
                source=task.source,
                job_kickoff=job_kickoff,
                cadence=cadence,
                task=task.task,
                success=False,
                error=error,
            )
        )
        failures.append(Failure(task.task, error, source=task.source))
        results.append(
            TaskResult(
                task=task.task,
                success=False,
                error=error,
                duration_seconds=command_result.duration_seconds,
                source=task.source,
            )
        )


def _group_tasks(
    rows: Sequence[Mapping[str, Any] | Task],
    order: Sequence[str] | None,
) -> tuple[dict[str, list[Task]], list[Mapping[str, Any] | Task]]:
    groups: dict[str, list[Task]] = {}
    invalid: list[Mapping[str, Any] | Task] = []

    for raw in rows:
        try:
            task = _require_task(raw)
        except (TypeError, ValueError):
            invalid.append(raw)
            continue
        groups.setdefault(task.source, []).append(task)

    for tasks in groups.values():
        tasks.sort(
            key=lambda task: (
                task.sequence is None,
                task.sequence if task.sequence is not None else 0,
                task.task,
            )
        )

    ordered: dict[str, list[Task]] = {}
    for name in _order_sources(list(groups), order):
        ordered[name] = groups[name]
    return ordered, invalid


def _order_sources(sources: Sequence[str], order: Sequence[str] | None) -> list[str]:
    if not order:
        return sorted(sources)
    indexes = {name: index for index, name in enumerate(order)}
    return sorted(sources, key=lambda name: (indexes.get(name, len(order)), name))


def _require_target(targets: Mapping[str, Target], source: str) -> Target:
    try:
        return targets[source]
    except KeyError as exc:
        raise ValueError(f"no target configured for source {source!r}") from exc


def _safe_write(
    row: dict[str, Any],
    write_log: WriteLog,
    log_write_errors: list[str],
) -> None:
    try:
        write_log(row)
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.exception("could not write update_log row for %s", row.get("task"))
        log_write_errors.append(message)


def _finish(
    result: RunResult,
    failures: list[Failure],
    log_write_errors: list[str],
    alert: Alert | None,
) -> None:
    if log_write_errors:
        failures.append(
            Failure(
                "util.update_log",
                f"{len(log_write_errors)} log write(s) failed; "
                f"first error: {log_write_errors[0]}",
            )
        )

    if failures and alert is not None:
        try:
            alert(failures)
        except Exception:
            logger.exception("could not send failure alert")

    result.failures.extend(failures)
    result.log_write_errors.extend(log_write_errors)


def _base_env(source: str, cadence: str, kickoff: datetime) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        {
            "UPDATE_LOG_SOURCE": source,
            "UPDATE_LOG_KICKOFF": kickoff.isoformat(),
            "SCS_CADENCE": cadence,
            "SCS_RUN_SOURCE": source,
        }
    )
    return env


def _target_label(target: Target | None) -> str:
    if target is None:
        return "<no target>"
    if target.is_remote:
        return f"{target.host}:{target.root}"
    return str(target.root)


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


def _raw_task_source(raw: Mapping[str, Any] | Task) -> str | None:
    if isinstance(raw, Task):
        return raw.source
    value = raw.get("source")
    return str(value).strip() or None if value is not None else None


def _aware(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _command_error(result: CommandResult, timeout: float | None) -> str:
    if result.timed_out:
        return f"timed out after {timeout}s" if timeout is not None else "timed out"
    if result.ssh_failed:
        message = "ssh failed"
    else:
        message = f"exit code {result.returncode}"
    output = result.output.strip()
    if output:
        message += f": {_tail(output)}"
    return message


def _tail(text: str, limit: int = 4000) -> str:
    text = text.strip()
    return text if len(text) <= limit else "..." + text[-limit:]
