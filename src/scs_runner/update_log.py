"""Build rows for ``util.update_log``.

The runner only builds dictionaries; the caller supplies the writer. That keeps
``scs_hub`` (SQLAlchemy/Polars) and the non-Python CLI (psycopg2) free to use
their own database layer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# Column order matches util.update_log.
UPDATE_LOG_COLUMNS = (
    "source",
    "job_kickoff",
    "cadence",
    "task",
    "task_time",
    "success",
    "error_message",
    "row_count",
)


def build_row(
    *,
    source: str,
    job_kickoff: datetime,
    cadence: str,
    task: str,
    success: bool,
    error: str | None = None,
    row_count: int | None = None,
    task_time: datetime | None = None,
) -> dict[str, Any]:
    """Return one row shaped exactly like ``util.update_log``."""
    message = str(error).strip() if error is not None else ""
    return {
        "source": source,
        "job_kickoff": job_kickoff,
        "cadence": cadence,
        "task": task,
        "task_time": task_time or datetime.now(UTC),
        "success": bool(success),
        "error_message": message or None,
        "row_count": int(row_count) if row_count is not None else None,
    }
