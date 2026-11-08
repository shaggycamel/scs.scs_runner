"""Run one command task locally or over SSH and capture its outcome."""

from __future__ import annotations

import json
import logging
import math
import os
import posixpath
import re
import shlex
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from scs_runner.models import Target, Task

logger = logging.getLogger(__name__)

# Extra local seconds allowed for the remote ``timeout`` to terminate the
# command and for ssh to notice. Local execution uses the timeout exactly.
_SSH_GRACE_SECONDS = 10
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Only these local values are forwarded to a remote task. Everything else is
# host-specific (credentials paths, PATH, proxies) and belongs in the Target or
# Task environment instead.
_REMOTE_ENV_KEYS = frozenset(
    {
        "SCS_TASK",
        "SCS_TASK_SOURCE",
        "SCS_CADENCE",
        "SCS_RUN_SOURCE",
        "UPDATE_LOG_SOURCE",
        "UPDATE_LOG_KICKOFF",
    }
)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    row_count: int | None = None
    ssh_failed: bool = False

    @property
    def success(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        """Combined stdout and stderr (stderr is merged into stdout)."""
        return self.stdout


def run_command(
    task: Task,
    *,
    target: Target,
    base_env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    result_file: Path | None = None,
) -> CommandResult:
    """Execute ``task.command`` at ``target``.

    Local targets run through the shell in ``target.root`` (plus
    ``task.working_dir``). Remote targets run the command over SSH from the
    remote root and use a remote temporary file for ``SCS_RESULT_FILE``.
    """
    env = _task_env(task, target, base_env)

    if target.is_remote:
        return _run_remote(task, target, env, timeout, result_file)

    cwd = _resolve_local_cwd(target.root, task.working_dir)
    local_result = None
    if result_file is not None:
        result_file.parent.mkdir(parents=True, exist_ok=True)
        result_file.unlink(missing_ok=True)
        local_result = str(result_file)
        env["SCS_RESULT_FILE"] = local_result

    returncode, output, timed_out, duration = _execute(
        task.command,
        shell=True,
        cwd=cwd,
        env=env,
        timeout=timeout,
        task_name=task.task,
    )
    row_count = None
    if returncode == 0 and not timed_out and result_file is not None:
        row_count = _read_result_file(result_file)
    return CommandResult(
        returncode=returncode,
        stdout=output,
        stderr="",
        duration_seconds=duration,
        timed_out=timed_out,
        row_count=row_count,
    )


def _task_env(
    task: Task,
    target: Target,
    base_env: Mapping[str, str] | None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env.setdefault("SCS_TASK", task.task)
    env["SCS_TASK_SOURCE"] = task.source
    # Nested log writers in the task should use the owning repo, not the run
    # source (which is the same repo for per-repo invocations).
    env["UPDATE_LOG_SOURCE"] = task.source
    env.update(target.environment)
    if task.environment:
        env.update(task.environment)
    return env


def _run_remote(
    task: Task,
    target: Target,
    env: Mapping[str, str],
    timeout: float | None,
    result_file: Path | None,
) -> CommandResult:
    remote_env = {key: value for key, value in env.items() if key in _REMOTE_ENV_KEYS}
    remote_env.update(target.environment)
    if task.environment:
        remote_env.update(task.environment)

    remote_result = None
    if result_file is not None:
        remote_result = f"/tmp/scs-runner-{uuid.uuid4().hex}.json"
        remote_env["SCS_RESULT_FILE"] = remote_result

    script, marker = _build_remote_script(
        task,
        target,
        remote_env,
        timeout,
        remote_result,
    )
    argv = [target.ssh_binary, *target.ssh_options, target.host or "", script]
    wait_timeout = None if timeout is None else timeout + _SSH_GRACE_SECONDS
    returncode, output, timed_out, duration = _execute(
        argv,
        shell=False,
        cwd=None,
        env=dict(os.environ),
        timeout=wait_timeout,
        task_name=task.task,
    )

    row_count = None
    if marker is not None:
        output, row_count = _extract_remote_result(
            output,
            marker,
            warn=returncode == 0 and not timed_out,
        )
    if returncode != 0 or timed_out:
        row_count = None

    return CommandResult(
        returncode=returncode,
        stdout=output,
        stderr="",
        duration_seconds=duration,
        timed_out=timed_out,
        row_count=row_count,
        ssh_failed=returncode == 255 and not timed_out,
    )


def _build_remote_script(
    task: Task,
    target: Target,
    env: Mapping[str, str],
    timeout: float | None,
    result_file: str | None,
) -> tuple[str, str | None]:
    """Return (script, result marker) for one SSH invocation."""
    root = str(target.root)
    cwd = (
        posixpath.normpath(posixpath.join(root, task.working_dir))
        if task.working_dir
        else root
    )

    lines = [f"cd {shlex.quote(cwd)} || exit 1"]
    for key, value in env.items():
        if not _ENV_KEY.match(key):
            raise ValueError(f"invalid environment variable name: {key!r}")
        lines.append(f"export {key}={shlex.quote(str(value))}")

    marker = None
    if result_file is not None:
        lines.append(f"rm -f {shlex.quote(result_file)}")
        marker = f"__SCS_RESULT__{uuid.uuid4().hex}__"

    command = task.command
    if timeout is not None:
        quoted = shlex.quote(command)
        command = (
            f"if command -v timeout >/dev/null 2>&1; then "
            f"timeout {math.ceil(timeout)} sh -c {quoted}; "
            f"else sh -c {quoted}; fi"
        )
    lines.append(command)
    lines.append("rc=$?")

    if result_file is not None and marker is not None:
        lines.append(
            f"if [ -f {shlex.quote(result_file)} ]; then "
            f"printf '\\n%s\\n' {shlex.quote(marker)}; "
            f"cat {shlex.quote(result_file)}; "
            f"rm -f {shlex.quote(result_file)}; fi"
        )
    lines.append("exit $rc")
    return "\n".join(lines), marker


def _execute(
    argv: str | list[str],
    *,
    shell: bool,
    cwd: Path | None,
    env: Mapping[str, str],
    timeout: float | None,
    task_name: str,
) -> tuple[int, str, bool, float]:
    started = time.monotonic()
    try:
        process = subprocess.Popen(
            argv,
            shell=shell,
            cwd=cwd,
            env=dict(env),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
    except OSError as exc:
        message = f"{type(exc).__name__}: {exc}"
        logger.error("could not start %s: %s", task_name, message)
        return 127, message, False, time.monotonic() - started

    chunks: list[str] = []

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            chunks.append(line)
            logger.info("[%s] %s", task_name, line.rstrip())

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()

    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(process)
    finally:
        reader.join(timeout=5)
        if process.stdout is not None:
            process.stdout.close()

    duration = time.monotonic() - started
    returncode = 124 if timed_out else int(process.returncode)
    return returncode, "".join(chunks), timed_out, duration


def _terminate(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _resolve_local_cwd(root: str | Path, working_dir: str | None) -> Path:
    base = Path(root).expanduser()
    if not base.is_absolute():
        base = Path.cwd() / base
    base = base.resolve()
    path = Path(working_dir) if working_dir else base
    if not path.is_absolute():
        path = base / path
    if not path.is_dir():
        raise FileNotFoundError(f"working directory does not exist: {path}")
    return path


def _read_result_file(path: Path) -> int | None:
    try:
        if not path.exists() or path.stat().st_size == 0:
            return None
        payload = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("could not read task result file %s: %s", path, exc)
        return None
    finally:
        path.unlink(missing_ok=True)
    return _parse_result_payload(payload)


def _extract_remote_result(
    output: str,
    marker: str,
    *,
    warn: bool = True,
) -> tuple[str, int | None]:
    needle = f"\n{marker}\n"
    index = output.rfind(needle)
    if index < 0:
        if warn:
            logger.warning("remote result marker not found in task output")
        return output, None
    clean = output[:index]
    payload = output[index + len(needle) :].strip()
    return clean, _parse_result_payload(payload)


def _parse_result_payload(payload: str) -> int | None:
    try:
        value = json.loads(payload)
    except ValueError as exc:
        logger.warning("task result file is not valid JSON: %s", exc)
        return None
    if not isinstance(value, dict):
        logger.warning("task result file must contain a JSON object")
        return None
    row_count = value.get("row_count")
    if row_count is None:
        return None
    try:
        return int(row_count)
    except (TypeError, ValueError):
        logger.warning("task result file has a non-integer row_count: %r", row_count)
        return None
