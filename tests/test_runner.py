from datetime import UTC, datetime

from scs_runner import run
from scs_runner.models import Task


def test_success_writes_start_task_and_end_rows(tmp_path):
    rows = []
    result = run(
        [Task(source="scs.test", task="ok", command="true")],
        source="scs.test",
        cadence="daily",
        write_log=rows.append,
        repo_root=tmp_path,
    )

    assert result.success
    assert [row["task"] for row in rows] == ["process start", "ok", "process end"]
    assert rows[0]["success"] is True
    assert rows[1]["success"] is True
    assert rows[1]["row_count"] is None
    assert rows[2]["success"] is True
    assert rows[1]["job_kickoff"] == result.job_kickoff


def test_failure_does_not_stop_later_tasks(tmp_path):
    rows = []
    result = run(
        [
            Task(
                source="scs.test",
                task="bad",
                command="sh -c 'echo boom >&2; exit 7'",
            ),
            Task(source="scs.test", task="good", command="true"),
        ],
        source="scs.test",
        cadence="daily",
        write_log=rows.append,
        repo_root=tmp_path,
    )

    assert not result.success
    assert [row["task"] for row in rows] == ["process start", "bad", "good", "process end"]
    assert rows[1]["success"] is False
    assert "exit code 7" in rows[1]["error_message"]
    assert "boom" in rows[1]["error_message"]
    assert rows[2]["success"] is True
    assert [failure.task for failure in result.failures] == ["bad"]


def test_row_count_is_read_from_result_file(tmp_path):
    rows = []
    result = run(
        [
            Task(
                source="scs.test",
                task="counted",
                command="""printf '{"row_count": 7}' > "$SCS_RESULT_FILE" """,
            )
        ],
        source="scs.test",
        cadence="daily",
        write_log=rows.append,
        repo_root=tmp_path,
    )

    assert result.success
    assert result.results[0].row_count == 7
    assert rows[1]["row_count"] == 7


def test_timeout_marks_task_failed(tmp_path):
    rows = []
    result = run(
        [
            Task(
                source="scs.test",
                task="slow",
                command="sleep 2",
                timeout_seconds=1,
            )
        ],
        source="scs.test",
        cadence="daily",
        write_log=rows.append,
        repo_root=tmp_path,
    )

    assert not result.success
    assert result.failures[0].task == "slow"
    assert "timed out" in rows[1]["error_message"].lower()


def test_log_write_failure_fails_the_run(tmp_path):
    rows = []
    calls = {"count": 0}

    def write_log(row):
        calls["count"] += 1
        if calls["count"] == 2:
            raise RuntimeError("log unavailable")
        rows.append(row)

    result = run(
        [Task(source="scs.test", task="ok", command="true")],
        source="scs.test",
        cadence="daily",
        write_log=write_log,
        repo_root=tmp_path,
    )

    assert not result.success
    assert result.log_write_errors
    assert any(failure.task == "util.update_log" for failure in result.failures)
    assert rows[-1]["task"] == "process end"


def test_alert_receives_all_failures_once(tmp_path):
    alerts = []
    result = run(
        [
            Task(source="scs.test", task="bad", command="false"),
            Task(source="scs.test", task="also-bad", command="false"),
        ],
        source="scs.test",
        cadence="daily",
        write_log=[].append,
        alert=alerts.append,
        repo_root=tmp_path,
    )

    assert not result.success
    assert len(alerts) == 1
    assert [failure.task for failure in alerts[0]] == ["bad", "also-bad"]


def test_paused_task_is_skipped_unless_included(tmp_path):
    task = Task(source="scs.test", task="paused", command="false", pause=True)

    rows = []
    result = run(
        [task],
        source="scs.test",
        cadence="daily",
        write_log=rows.append,
        repo_root=tmp_path,
    )
    assert result.success
    assert [row["task"] for row in rows] == ["process start", "process end"]

    rows = []
    result = run(
        [task],
        source="scs.test",
        cadence="daily",
        write_log=rows.append,
        repo_root=tmp_path,
        include_paused=True,
    )
    assert not result.success
    assert rows[1]["task"] == "paused"


def test_working_dir_and_environment_are_applied(tmp_path):
    sub = tmp_path / "sub"
    sub.mkdir()
    task = Task(
        source="scs.test",
        task="env",
        command='printf "%s" "$FOO" > out.txt',
        working_dir="sub",
        environment={"FOO": "bar"},
    )

    result = run(
        [task],
        source="scs.test",
        cadence="daily",
        write_log=[].append,
        repo_root=tmp_path,
    )

    assert result.success
    assert (sub / "out.txt").read_text() == "bar"


def test_invalid_row_is_logged_and_does_not_crash_run(tmp_path):
    rows = []
    result = run(
        [{"source": "scs.test", "task": "broken"}],
        source="scs.test",
        cadence="daily",
        write_log=rows.append,
        repo_root=tmp_path,
    )

    assert not result.success
    assert result.failures[0].task == "broken"
    assert rows[1]["success"] is False
    assert "command" in rows[1]["error_message"]


def test_dry_run_does_not_write_or_execute(tmp_path):
    rows = []
    result = run(
        [Task(source="scs.test", task="dry", command="false")],
        source="scs.test",
        cadence="daily",
        write_log=rows.append,
        repo_root=tmp_path,
        dry_run=True,
    )

    assert result.dry_run
    assert result.success
    assert rows == []


def test_task_source_overrides_run_source(tmp_path):
    rows = []
    run(
        [Task(source="other.repo", task="one", command="true")],
        source="scs.runner",
        cadence="daily",
        write_log=rows.append,
        repo_root=tmp_path,
    )

    assert rows[0]["source"] == "scs.runner"
    assert rows[1]["source"] == "other.repo"
    assert rows[2]["source"] == "scs.runner"


def test_task_env_uses_the_task_source(tmp_path):
    task = Task(
        source="other.repo",
        task="env-source",
        command='printf "%s" "$UPDATE_LOG_SOURCE" > source.txt',
    )

    result = run(
        [task],
        source="scs.runner",
        cadence="daily",
        write_log=[].append,
        repo_root=tmp_path,
    )

    assert result.success
    assert (tmp_path / "source.txt").read_text() == "other.repo"


def test_naive_kickoff_is_treated_as_utc(tmp_path):
    result = run(
        [Task(source="scs.test", task="one", command="true")],
        source="scs.test",
        cadence="daily",
        write_log=[].append,
        repo_root=tmp_path,
        job_kickoff=datetime(2026, 1, 1),
    )

    assert result.job_kickoff.tzinfo is UTC
