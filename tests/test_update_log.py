from datetime import UTC, datetime

from scs_runner.update_log import UPDATE_LOG_COLUMNS, build_row


def test_build_row_has_update_log_columns_and_normalises_error():
    kickoff = datetime(2026, 1, 1, tzinfo=UTC)
    row = build_row(
        source="scs.test",
        job_kickoff=kickoff,
        cadence="daily",
        task="task",
        success=True,
        error="  ",
    )

    assert tuple(row) == UPDATE_LOG_COLUMNS
    assert row["error_message"] is None
    assert row["row_count"] is None
    assert row["job_kickoff"] is kickoff
    assert row["task_time"].tzinfo is not None
