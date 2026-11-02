"""``scs-run``: standalone entry point for non-Python repos.

The CLI deliberately owns its small Postgres adapter. Python repos use
``scs_hub`` to query and write logs, then call ``scs_runner.run`` themselves.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scs_runner.alerts import failure_alert, send_failure_email
from scs_runner.config import credentials_path, read_section
from scs_runner.models import Failure
from scs_runner.runner import run

logger = logging.getLogger(__name__)

DEFAULT_DB_SECTION = "cockroach-write"
DEFAULT_SOURCE = "scs.runner"

_INSERT_UPDATE_LOG = """
    INSERT INTO util.update_log
        (source, job_kickoff, cadence, task, task_time,
         success, error_message, row_count)
    VALUES
        (%(source)s, %(job_kickoff)s, %(cadence)s, %(task)s, %(task_time)s,
         %(success)s, %(error_message)s, %(row_count)s)
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scs-run",
        description="Run the util.update_schedule tasks for one repo.",
    )
    parser.add_argument(
        "cadences",
        nargs="*",
        help="Cadence filter(s), space- or comma-separated; bare run means all.",
    )
    parser.add_argument("--source", help="Repo name recorded in util.update_log")
    parser.add_argument(
        "--task",
        action="append",
        default=[],
        metavar="TASK",
        help="Run a specific task (repeatable); ignores cadence and pause.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Ignore the cadence filter and run every unpaused task.",
    )
    parser.add_argument(
        "--db-section",
        default=os.environ.get("SCS_RUNNER_DB_SECTION", DEFAULT_DB_SECTION),
        help=f"Credentials section (default: {DEFAULT_DB_SECTION})",
    )
    parser.add_argument("--credentials", metavar="PATH", help="Credentials INI path")
    parser.add_argument("--list", action="store_true", help="List selected tasks")
    parser.add_argument("--dry-run", action="store_true", help="Print tasks without running")
    parser.add_argument("--timeout", type=int, help="Default per-task timeout in seconds")
    parser.add_argument("--no-alert", action="store_true", help="Do not send failure email")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    return parser


def parse_cadences(values: list[str]) -> list[str]:
    """Space- or comma-separated cadences, deduplicated in order."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in value.replace(",", " ").split():
            if part not in seen:
                seen.add(part)
                out.append(part)
    return out


def resolve_source(explicit: str | None, repo_root: Path) -> str:
    """Explicit arg, then env, then the checkout's git remote, then a fallback."""
    if explicit and explicit.strip():
        return explicit.strip()

    from_env = os.environ.get("UPDATE_LOG_SOURCE")
    if from_env and from_env.strip():
        return from_env.strip()

    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None

    if completed is not None and completed.returncode == 0:
        remote = completed.stdout.strip().rstrip("/")
        name = remote.rsplit("/", 1)[-1]
        name = name.removesuffix(".git")
        if name:
            return name
    return DEFAULT_SOURCE


def parse_kickoff() -> datetime:
    raw = os.environ.get("UPDATE_LOG_KICKOFF")
    if raw:
        try:
            value = datetime.fromisoformat(raw)
        except ValueError:
            logger.warning("could not parse UPDATE_LOG_KICKOFF=%r; using now", raw)
        else:
            return value if value.tzinfo else value.replace(tzinfo=UTC)
    return datetime.now(UTC)


def connect(section: str, ini_path: Path) -> Any:
    """Open a psycopg2 connection from the credentials INI."""
    import psycopg2

    settings = read_section(section, ini_path)
    kwargs: dict[str, Any] = {
        "user": settings.get("user"),
        "password": settings.get("password"),
        "host": settings.get("host"),
        "dbname": settings.get("database") or settings.get("dbname"),
        "connect_timeout": 30,
        "application_name": "scs-runner",
    }
    if settings.get("port"):
        kwargs["port"] = int(settings["port"])
    for key in ("sslmode", "sslrootcert", "sslcert", "sslkey", "options"):
        if settings.get(key):
            kwargs[key] = settings[key]
    return psycopg2.connect(**{key: value for key, value in kwargs.items() if value is not None})


def query_tasks(
    conn: Any,
    *,
    source: str,
    cadences: list[str],
    tasks: list[str],
    include_paused: bool,
) -> list[dict[str, Any]]:
    clauses = ["source = %(source)s"]
    params: dict[str, Any] = {"source": source}

    if tasks:
        clauses.append("task = ANY(%(tasks)s)")
        params["tasks"] = list(tasks)
    else:
        if not include_paused:
            clauses.append("pause IS NOT TRUE")
        if cadences:
            clauses.append("cadence = ANY(%(cadences)s)")
            params["cadences"] = list(cadences)

    sql = f"""
        SELECT source, task, command, cadence, sequence, pause,
               working_dir, environment, timeout_seconds
        FROM util.update_schedule
        WHERE {' AND '.join(clauses)}
        ORDER BY sequence NULLS LAST, task
    """
    with conn.cursor() as cursor:
        cursor.execute(sql, params)
        columns = [description[0] for description in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def make_log_writer(conn: Any):
    def write_log(row: dict[str, Any]) -> None:
        try:
            with conn.cursor() as cursor:
                cursor.execute(_INSERT_UPDATE_LOG, row)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    return write_log


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cadences = [] if args.all else parse_cadences(args.cadences)
    cadence_label = ",".join(cadences) if cadences else "all"
    repo_root = Path.cwd()
    source = resolve_source(args.source, repo_root)
    ini_path = credentials_path(args.credentials)
    job_kickoff = parse_kickoff()
    include_paused = bool(args.task)

    try:
        conn = connect(args.db_section, ini_path)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        logger.exception("could not connect to database")
        if not args.no_alert:
            send_failure_email(
                [Failure("util.update_schedule", error)],
                source=source,
                ini_path=ini_path,
            )
        return 1

    try:
        try:
            tasks = query_tasks(
                conn,
                source=source,
                cadences=cadences,
                tasks=args.task,
                include_paused=include_paused,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            logger.exception("could not read util.update_schedule")
            if not args.no_alert:
                send_failure_email(
                    [Failure("util.update_schedule", error)],
                    source=source,
                    ini_path=ini_path,
                )
            return 1

        if args.list:
            if not tasks:
                print(f"No tasks selected for source={source} cadence={cadence_label}")
                return 0
            for task in tasks:
                print(
                    f"{task['task']}\t{task.get('cadence') or 'all'}\t"
                    f"{task.get('sequence') or ''}\t{task['command']}"
                )
            return 0

        if args.dry_run:
            if not tasks:
                print(f"No tasks selected for source={source} cadence={cadence_label}")
                return 0
            for task in tasks:
                print(f"[dry-run] {task['task']}: {task['command']}")
            return 0

        if not tasks:
            logger.warning("no tasks selected for source=%s cadence=%s", source, cadence_label)
            return 0

        alert = None if args.no_alert else failure_alert(source, ini_path=ini_path)
        result = run(
            tasks,
            source=source,
            cadence=cadence_label,
            write_log=make_log_writer(conn),
            job_kickoff=job_kickoff,
            alert=alert,
            repo_root=repo_root,
            default_timeout=args.timeout,
            include_paused=include_paused,
        )

        if result.success:
            logger.info("all %d task(s) successfully completed", len(result.results))
            return 0

        logger.error("failed tasks: %s", [failure.task for failure in result.failures])
        return result.exit_code
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
