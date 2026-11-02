from scs_runner.models import Task


def test_from_row_normalises_values():
    task = Task.from_row(
        {
            "source": " scs.test ",
            "task": " test.task ",
            "command": " true ",
            "cadence": "",
            "sequence": "10",
            "pause": None,
            "working_dir": "",
            "environment": '{"FOO": "bar", "N": 1}',
            "timeout_seconds": "30",
        }
    )

    assert task.source == "scs.test"
    assert task.task == "test.task"
    assert task.command == "true"
    assert task.cadence is None
    assert task.sequence == 10
    assert task.pause is False
    assert task.working_dir is None
    assert task.environment == {"FOO": "bar", "N": "1"}
    assert task.timeout_seconds == 30


def test_from_row_parses_string_booleans():
    task = Task.from_row(
        {"source": "s", "task": "t", "command": "true", "pause": "false"}
    )
    assert task.pause is False


def test_from_row_requires_source_task_and_command():
    for field in ("source", "task", "command"):
        row = {"source": "s", "task": "t", "command": "true"}
        row[field] = None
        try:
            Task.from_row(row)
        except ValueError as exc:
            assert field in str(exc)
        else:
            raise AssertionError(f"{field} should be required")
