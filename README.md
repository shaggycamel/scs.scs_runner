# scs-runner

Small, language-agnostic task runner driven by `util.update_schedule`.

A repo can query its own schedule and call `run()`, or a central runner can
query several sources and call `run_sources()` with local or SSH targets.
Python repos commonly use `scs_hub` for the query and log write; non-Python
repos can use the `scs-run` CLI. All paths hand the selected rows to the same
execution core, which:

- writes `process start` / `process end` to `util.update_log`;
- executes each task command, continuing after failures;
- writes one `util.update_log` row per task;
- optionally sends one failure email;
- exits 0 or 1.

The runner does not depend on `scs_hub`, `polars`, `nba_api`, or `nbainjuries`.

## Install

```bash
cd scs_runner
uv sync --extra dev
```

Or install the package in another environment:

```bash
pip install .
```

The CLI needs Python 3.11+ and a Postgres driver. `psycopg2-binary` is the
default. On hosts where that wheel is unavailable, use a pure-Python driver such
as `pg8000` and adapt `connect()` in `src/scs_runner/cli.py`.

## CLI

Run from the repo root:

```bash
scs-run daily
scs-run daily,intraday
scs-run daily intraday
scs-run --task mod.pts.predict
scs-run --source scs.nba.mod.pts --all
scs-run --list
scs-run --dry-run
```

`source` is resolved from `--source`, then `UPDATE_LOG_SOURCE`, then the
checkout's git remote, then `scs.runner`.

The CLI reads the credentials file from `SCS_HUB_CREDENTIALS` or
`~/.config/scs_hub_credentials.ini`, and uses `--db-section` (default
`cockroach-write`).

## Schedule row

The runner expects these columns:

```text
source           repo/owner, e.g. scs.nba.mod.pts
task             util.update_log.task
command          shell command, relative to the repo root
cadence          daily / intraday / ...
sequence         ordering within source
pause            skip unless explicitly selected
working_dir      optional, relative to repo root
environment      optional JSON object of extra env vars
timeout_seconds  optional per-task timeout
```

A row looks like:

```text
source      scs.nba.mod.pts
task        mod.pts.predict
command     Rscript modelling/mod_exec.R
cadence     daily
sequence    10
```

## Python repo

```python
import sys

import polars as pl
from scs_hub import SportsHub
from scs_runner import run

hub = SportsHub("cockroach-write")

cadences = sys.argv[1:]
params = {"source": "scs.nba.mod.pts"}
where = "source = :source AND pause IS NOT TRUE"
if cadences:
    placeholders = ", ".join(f":c{i}" for i in range(len(cadences)))
    where += f" AND cadence IN ({placeholders})"
    params.update({f"c{i}": c for i, c in enumerate(cadences)})

tasks = hub.db.read(
    f"""
    SELECT source, task, command, cadence, sequence, pause,
           working_dir, environment, timeout_seconds
    FROM util.update_schedule
    WHERE {where}
    ORDER BY sequence NULLS LAST, task
    """,
    execute_options={"parameters": params},
).to_dicts()


def write_log(row):
    hub.db.write(
        pl.DataFrame([row], schema_overrides={"row_count": pl.Int64}),
        "update_log",
        "util",
    )


result = run(
    tasks,
    source="scs.nba.mod.pts",
    cadence=",".join(cadences) or "all",
    write_log=write_log,
)
raise SystemExit(result.exit_code)
```

## Multiple sources and SSH

`run_sources()` groups rows by `source`, orders the groups, and executes each at
its own `Target`. A target is local (`host=None`) or remote:

```python
from scs_runner import Target, Task, run_sources

tasks = [
    Task(source="scs.nba.mod.player_archetypes", task="player_archetypes.job",
         command="uv run --frozen --group data python __main__.py"),
]
targets = {
    "scs.nba.mod.player_archetypes": Target(
        host="jetson",
        root="/home/oli/shaggy_camel_sports/nba/mod/player_archetypes",
        ssh_options=("-o", "BatchMode=yes"),
        environment={"PATH": "/home/oli/.local/bin:/usr/bin:/bin"},
    ),
}

result = run_sources(
    tasks,
    source="scs.nba.mod.runner",
    cadence="weekly",
    write_log=write_log,
    targets=targets,
    bookends="source",
)
```

Remote execution:

- runs `ssh <options> <host> '<script>'` with `shlex`-quoted `cd`, env and command;
- uses the remote `timeout` command when available;
- sets `SCS_RESULT_FILE` to a remote temp file and reads it back with a marker;
- distinguishes an SSH failure (`255`) from the command's exit code;
- streams output to the runner log.

`bookends="source"` writes `process start` / `process end` per source;
`bookends="run"` writes one envelope under the run source; `bookends="none"`
writes only task rows.

The `nba/mod` runner derives `source`, `task`, `command` and `Target` from
`util.update_schedule.job_family = 'model'` and `associated_function`.

## Non-Python repo

`scs-run` does the same query and log writing with psycopg2, then executes the
commands. An R or shell repo only needs a schedule row and a cron wrapper:

```bash
#!/bin/bash
cd "$(dirname "$0")" || exit 1
exec scs-run daily
```

## Row counts

A command can optionally report how many rows it wrote:

```bash
printf '{"row_count": 42}' > "$SCS_RESULT_FILE"
```

The runner reads `SCS_RESULT_FILE` after a successful task. If the file is
absent or has no `row_count`, the log row keeps `row_count = NULL`.

The runner also sets these env vars for each command:

- `SCS_TASK`
- `SCS_TASK_SOURCE`
- `SCS_CADENCE`
- `SCS_RUN_SOURCE`
- `UPDATE_LOG_SOURCE`
- `UPDATE_LOG_KICKOFF`
- `SCS_RESULT_FILE`

## Failure alerts

`[smtp]` in the credentials file is used when `user` and `password` are set.
Environment overrides: `SMTP_USER`, `SMTP_PASSWORD`, `ALERT_TO`, `SMTP_HOST`,
`SMTP_PORT`. Without them, the failure email is skipped and the run still exits
1.

## Tests

```bash
uv run pytest
```

See [PLAN.md](PLAN.md) for the proposed `util.update_schedule` migration and the
database/mod integration work.
