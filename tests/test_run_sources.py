from scs_runner import run_sources
from scs_runner.execute import _build_remote_script, _extract_remote_result
from scs_runner.models import Target, Task


def test_run_sources_uses_each_source_target_and_writes_bookends(tmp_path):
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()

    rows = []
    result = run_sources(
        [
            Task(
                source="scs.alpha",
                task="one",
                command="printf a > out.txt",
                sequence=10,
            ),
            Task(
                source="scs.beta", task="two", command="printf b > out.txt", sequence=10
            ),
        ],
        source="scs.run",
        cadence="weekly",
        write_log=rows.append,
        targets={
            "scs.alpha": Target.local(alpha),
            "scs.beta": Target.local(beta),
        },
        order=["scs.beta", "scs.alpha"],
    )

    assert result.success
    assert (alpha / "out.txt").read_text() == "a"
    assert (beta / "out.txt").read_text() == "b"
    assert [(row["source"], row["task"]) for row in rows] == [
        ("scs.beta", "process start"),
        ("scs.beta", "two"),
        ("scs.beta", "process end"),
        ("scs.alpha", "process start"),
        ("scs.alpha", "one"),
        ("scs.alpha", "process end"),
    ]
    assert [item.source for item in result.results] == ["scs.beta", "scs.alpha"]


def test_run_sources_missing_target_fails_only_that_source(tmp_path):
    good = tmp_path / "good"
    good.mkdir()

    rows = []
    result = run_sources(
        [
            Task(source="scs.missing", task="broken", command="true"),
            Task(source="scs.good", task="ok", command="true"),
        ],
        source="scs.run",
        cadence="weekly",
        write_log=rows.append,
        targets={"scs.good": Target.local(good)},
    )

    assert not result.success
    assert [failure.task for failure in result.failures] == ["broken"]
    assert "no target configured" in result.failures[0].error
    assert any(row["task"] == "ok" and row["success"] for row in rows)


def test_run_sources_can_use_one_run_envelope(tmp_path):
    rows = []
    result = run_sources(
        [
            Task(source="scs.alpha", task="one", command="true"),
            Task(source="scs.beta", task="two", command="true"),
        ],
        source="scs.run",
        cadence="weekly",
        write_log=rows.append,
        targets={
            "scs.alpha": Target.local(tmp_path),
            "scs.beta": Target.local(tmp_path),
        },
        bookends="run",
    )

    assert result.success
    assert [(row["source"], row["task"]) for row in rows] == [
        ("scs.run", "process start"),
        ("scs.alpha", "one"),
        ("scs.beta", "two"),
        ("scs.run", "process end"),
    ]


def test_run_sources_dry_run_writes_nothing(tmp_path):
    rows = []
    result = run_sources(
        [Task(source="scs.alpha", task="one", command="false")],
        source="scs.run",
        cadence="weekly",
        write_log=rows.append,
        targets={"scs.alpha": Target.local(tmp_path)},
        dry_run=True,
    )

    assert result.dry_run
    assert result.success
    assert rows == []


def test_remote_script_sets_cwd_env_timeout_and_result_marker():
    task = Task(
        source="scs.test",
        task="remote",
        command="python __main__.py",
        working_dir="sub",
        environment={"FOO": "bar"},
    )
    script, marker = _build_remote_script(
        task,
        Target(host="jetson", root="/repo"),
        {"SCS_TASK": "remote", "PATH": "/usr/bin"},
        30,
        "/tmp/result.json",
    )

    assert marker is not None
    assert "cd /repo/sub" in script
    assert "export SCS_TASK=remote" in script
    assert "export PATH=/usr/bin" in script
    assert "timeout 30 sh -c" in script
    assert marker in script
    assert "/tmp/result.json" in script


def test_remote_result_marker_is_removed_from_output():
    output = 'line one\n\n__SCS_RESULT__abc__\n{"row_count": 5}\n'
    clean, row_count = _extract_remote_result(output, "__SCS_RESULT__abc__")

    assert clean == "line one\n"
    assert row_count == 5


def test_ssh_failure_is_reported_as_ssh_failed(tmp_path):
    fake_ssh = tmp_path / "fail-ssh"
    fake_ssh.write_text(
        "#!/bin/sh\necho 'ssh: connect to host failed' >&2\nexit 255\n",
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    result = run_sources(
        [Task(source="scs.remote", task="remote.task", command="true")],
        source="scs.run",
        cadence="weekly",
        write_log=[].append,
        targets={
            "scs.remote": Target(
                host="fake-host",
                root="/repo",
                ssh_binary=str(fake_ssh),
            )
        },
    )

    assert not result.success
    assert "ssh failed" in result.failures[0].error
    assert "connect to host failed" in result.failures[0].error


def test_ssh_target_round_trips_result_file(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    fake_ssh = tmp_path / "fake-ssh"
    fake_ssh.write_text(
        '#!/bin/sh\nfor last; do :; done\nexec sh -c "$last"\n',
        encoding="utf-8",
    )
    fake_ssh.chmod(0o755)

    rows = []
    result = run_sources(
        [
            Task(
                source="scs.remote",
                task="remote.task",
                command='printf \'{"row_count": 3}\' > "$SCS_RESULT_FILE"',
            )
        ],
        source="scs.run",
        cadence="weekly",
        write_log=rows.append,
        targets={
            "scs.remote": Target(
                host="fake-host",
                root=repo,
                ssh_binary=str(fake_ssh),
            )
        },
    )

    assert result.success
    assert result.results[0].row_count == 3
    assert rows[1]["task"] == "remote.task"
    assert rows[1]["row_count"] == 3
