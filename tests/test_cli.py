from datetime import UTC

from scs_runner.cli import parse_cadences, parse_kickoff, query_tasks, resolve_source


class FakeCursor:
    def __init__(self):
        self.sql = None
        self.params = None
        self.description = [
            ("source",),
            ("task",),
            ("command",),
            ("cadence",),
            ("sequence",),
            ("pause",),
            ("working_dir",),
            ("environment",),
            ("timeout_seconds",),
        ]

    def execute(self, sql, params):
        self.sql = sql
        self.params = params

    def fetchall(self):
        return [
            (
                "scs.test",
                "one",
                "true",
                "daily",
                10,
                False,
                None,
                None,
                None,
            )
        ]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeConnection:
    def __init__(self):
        self.cursor_instance = FakeCursor()

    def cursor(self):
        return self.cursor_instance


def test_parse_cadences_splits_and_deduplicates():
    assert parse_cadences(["daily,intraday", "daily weekly"]) == [
        "daily",
        "intraday",
        "weekly",
    ]


def test_resolve_source_prefers_explicit_and_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UPDATE_LOG_SOURCE", "from.env")
    assert resolve_source("explicit", tmp_path) == "explicit"
    assert resolve_source(None, tmp_path) == "from.env"


def test_resolve_source_falls_back_without_git(monkeypatch, tmp_path):
    monkeypatch.delenv("UPDATE_LOG_SOURCE", raising=False)
    assert resolve_source(None, tmp_path) == "scs.runner"


def test_parse_kickoff_reads_iso_env(monkeypatch):
    monkeypatch.setenv("UPDATE_LOG_KICKOFF", "2026-01-01T12:00:00Z")
    kickoff = parse_kickoff()
    assert kickoff.tzinfo is UTC
    assert kickoff.hour == 12


def test_query_tasks_filters_cadence_and_pause():
    conn = FakeConnection()
    tasks = query_tasks(
        conn,
        source="scs.test",
        cadences=["daily", "intraday"],
        tasks=[],
        include_paused=False,
    )

    assert tasks[0]["task"] == "one"
    assert "pause IS NOT TRUE" in conn.cursor_instance.sql
    assert "cadence = ANY" in conn.cursor_instance.sql
    assert conn.cursor_instance.params == {
        "source": "scs.test",
        "cadences": ["daily", "intraday"],
    }


def test_query_tasks_manual_selection_ignores_pause_and_cadence():
    conn = FakeConnection()
    query_tasks(
        conn,
        source="scs.test",
        cadences=["daily"],
        tasks=["one"],
        include_paused=True,
    )

    assert "pause IS NOT TRUE" not in conn.cursor_instance.sql
    assert "cadence = ANY" not in conn.cursor_instance.sql
    assert conn.cursor_instance.params == {
        "source": "scs.test",
        "tasks": ["one"],
    }


def test_query_tasks_bare_run_has_no_cadence_clause():
    conn = FakeConnection()
    query_tasks(
        conn,
        source="scs.test",
        cadences=[],
        tasks=[],
        include_paused=False,
    )

    assert "cadence = ANY" not in conn.cursor_instance.sql
    assert "pause IS NOT TRUE" in conn.cursor_instance.sql
