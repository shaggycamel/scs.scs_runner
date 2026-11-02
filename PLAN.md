# scs_runner + util.update_schedule plan

> **Implementation status:** the `scs_runner` package in this repo is implemented.
> The `util.update_schedule` migration and the database/mod repo changes below
> are **not applied**; they remain the next steps.

## Goal

Create a small, language-agnostic task runner, `scs_runner`, used by both Python
and non-Python repos. Each repo queries `util.update_schedule` itself, then hands
the selected rows to the runner. The runner executes each row's command, writes
`util.update_log`, alerts on failure, and exits 0/1.

## Decisions already made

- Per-repo invocation, not one central orchestrator.
- Commands live in `util.update_schedule`.
- `scs_hub` stays a generic DB layer. No `scs_hub.schedule` module; Python repos
  use `hub.db.read(...)` / `hub.db.write(...)` directly.
- Schedule SQL is inline in each repo's entry point for now. The runner package
  does not own a query builder.
- Python repos use `scs_hub` to query and write logs.
- Non-Python repos use the `scs-run` CLI with its own small Postgres adapter.
- The runner must not depend on `scs_hub`.
- `fty` reconnecting per task is accepted.
- League dash keeps its existing `deploy/cron.sh` and R logger for now.

## Location and layout

Project root:

```text
/Users/fred/shaggy_camel_sports/scs_runner
```

Initial layout:

```text
scs_runner/
├── pyproject.toml
├── README.md
├── .gitignore
├── src/scs_runner/
│   ├── __init__.py          # public API: run, RunResult
│   ├── __main__.py          # python -m scs_runner
│   ├── config.py            # credentials INI path/section
│   ├── models.py            # Task row mapping / RunResult
│   ├── runner.py            # loop, bookends, failure aggregation, exit
│   ├── execute.py           # subprocess: cwd, env, timeout, stderr capture
│   ├── update_log.py        # update_log row keys + writer protocol
│   ├── alerts.py            # SMTP failure summary
│   └── cli.py               # scs-run: inline SQL + psycopg2
└── tests/
```

`pyproject.toml`:

- package name `scs-runner`
- import package `scs_runner`
- console script `scs-run = "scs_runner.cli:main"`
- Python `>=3.11`
- runtime dependency: a Postgres driver (`psycopg2-binary`; `pg8000` fallback on
  ARM if binary wheels are a problem)
- no `scs_hub`, `polars`, `nba_api`, or `nbainjuries` dependency

## Runner contract

```python
run(
    tasks,                 # iterable of mappings from update_schedule
    *,
    source: str,           # repo writing update_log
    cadence: str,          # run cadence label
    write_log,             # callable(row: dict) -> None
    job_kickoff=None,      # datetime; defaults to now UTC
    alert=None,            # optional (failures) -> None callback
    dry_run=False,
    result_file_env="SCS_RESULT_FILE",
) -> RunResult
```

Behaviour:

1. Write a `process start` row with `success=True`, `row_count=None`.
2. For each task, in the order supplied:
   - skip `pause` rows unless an explicit manual selection was made;
   - execute `command` with `cwd=working_dir or repo root`, merged `environment`;
   - apply `timeout_seconds` if set;
   - on success, read optional JSON result from `SCS_RESULT_FILE` for `row_count`;
   - write one `util.update_log` row:
     `source`, `job_kickoff`, `cadence`, `task`, `task_time`, `success`,
     `error_message`, `row_count`;
   - on failure, record the error, continue to the next task.
3. Write a `process end` row with `success = no failures`.
4. If any log write fails, add that to the failure list.
5. Call `alert(failures)` once if anything failed.
6. Return a result object containing successes, failures and row counts.

Rows passed to `write_log` must already have the exact `util.update_log` columns
so both the Python and non-Python writers are trivial.

## Python repo entry point

Example (`nba/database/run.py`, and a similar one for mod Python repos):

```python
hub = SportsHub(db_con=DB_CON, ini_path=INI_PATH)

tasks = hub.db.read(
    """
    SELECT source, task, command, cadence, sequence, pause,
           working_dir, environment, timeout_seconds
    FROM util.update_schedule
    WHERE source = :source
      AND pause IS NOT TRUE
      AND cadence IN (:c0, :c1)
    ORDER BY sequence NULLS LAST, task
    """,
    execute_options={"parameters": params},
).to_dicts()

scs_runner.run(
    tasks,
    source="scs.nba.database",
    cadence=run_cadence,
    write_log=write_log,
)
```

`write_log`:

```python
def write_log(row):
    hub.db.write(
        pl.DataFrame([row], schema_overrides={"row_count": pl.Int64}),
        "update_log",
        "util",
    )
```

## Non-Python entry point

`scs-run daily` is run from the repo root. It:

- reads credentials from `SCS_HUB_CREDENTIALS` or
  `~/.config/scs_hub_credentials.ini`;
- uses `--db-section` (default `cockroach-write`) for query and log writes;
- selects rows with inline SQL;
- runs each command via the same `scs_runner.run` core;
- writes `update_log` via psycopg2;
- sends the failure email using the same `[smtp]` settings and env overrides.

CLI options:

```text
scs-run [cadence ...]
  --source SOURCE
  --task TASK            repeatable; manual selection
  --all                  ignore cadence filter
  --db-section SECTION
  --list
  --dry-run
  --timeout SECONDS      default per-task timeout
```

`source` resolution order: `--source`, `UPDATE_LOG_SOURCE`, git remote name,
fallback `scs.runner`.

## util.update_schedule changes

### Current schema

```sql
schema              VARCHAR
table_name          VARCHAR
associated_function VARCHAR
cadence             VARCHAR
schema_sequence     INTEGER
function_sequence   INTEGER
pause               BOOLEAN
```

### Target schema

```sql
source           VARCHAR NOT NULL,   -- repo: scs.nba.database, scs.nba.mod.pts
schema           VARCHAR,            -- optional metadata
table_name       VARCHAR,            -- optional metadata
task             VARCHAR NOT NULL,   -- update_log.task
command          TEXT NOT NULL,      -- shell command, relative to repo root
cadence          VARCHAR,
sequence         INTEGER,            -- ordering within source
pause            BOOLEAN DEFAULT FALSE,
working_dir      VARCHAR,            -- optional, relative to repo root
environment      JSONB,              -- optional extra env vars
timeout_seconds  INTEGER,            -- optional
PRIMARY KEY (source, task)
```

Notes:

- `associated_function` is replaced by `task` (same values for database rows).
- `schema_sequence` / `function_sequence` collapse into `sequence`.
- `schema` and `table_name` are retained only as metadata; the runner ignores
  them.
- `environment` and `timeout_seconds` can be left NULL for the first pass.
- `update_log` does not change.

### Migration outline

Run against POSTGRES only, then transport to Cockroach as with the existing
`sql/tables/util.update_schedule.sql` rebuild.

```sql
BEGIN;

ALTER TABLE util.update_schedule RENAME TO update_schedule_old;

CREATE TABLE util.update_schedule (
    source           VARCHAR NOT NULL,
    schema           VARCHAR,
    table_name       VARCHAR,
    task             VARCHAR NOT NULL,
    command          TEXT NOT NULL,
    cadence          VARCHAR,
    sequence         INTEGER,
    pause            BOOLEAN DEFAULT FALSE,
    working_dir      VARCHAR,
    environment      JSONB,
    timeout_seconds  INTEGER,
    PRIMARY KEY (source, task)
);

INSERT INTO util.update_schedule
    (source, schema, table_name, task, command, cadence, sequence, pause)
SELECT
    'scs.nba.database',
    schema,
    table_name,
    associated_function,
    'python task.py ' || associated_function,
    cadence,
    (row_number() OVER (
        ORDER BY schema_sequence NULLS LAST,
                 function_sequence NULLS LAST,
                 table_name
    ) * 10)::integer,
    COALESCE(pause, FALSE)
FROM util.update_schedule_old
WHERE associated_function IS NOT NULL;

DROP TABLE util.update_schedule_old;
COMMIT;
```

Then verify:

- row count before/after matches;
- every database `task` is unique within `source`;
- `sequence` preserves the old `schema_sequence, function_sequence, table_name`
  order;
- `cadence` values are as expected (backfill any NULLs before enforcing NOT NULL
  later, if desired).

### New rows

Mod `pts`:

```sql
INSERT INTO util.update_schedule
    (source, schema, table_name, task, command, cadence, sequence, pause)
VALUES
    ('scs.nba.mod.pts', 'anl', 'pts_prediction', 'mod.pts.predict',
     'Rscript modelling/mod_exec.R', 'daily', 10, FALSE);
```

Optional later, mod `scratch`:

```sql
INSERT INTO util.update_schedule
    (source, schema, table_name, task, command, cadence, sequence, pause)
VALUES
    ('scs.nba.mod.scratch', 'anl', 'usage_prediction', 'mod.scratch.predict',
     'uv run python modelling/predict.py', 'daily', 10, FALSE);
```

### Mirror and docs

- Update `nba/database/sql/tables/util.update_schedule.sql` to the new rebuild
  script.
- Update `nba/database/README.md` references to `schema_sequence`,
  `function_sequence`, and `associated_function`.
- Update comments in `database/__main__.py` / its replacement and any docs that
  describe the old columns.

## Database task CLI

Because `scs_runner` executes commands, database rows need a one-task command:

```text
python task.py nba.get_game_schedule
```

Add `nba/database/task.py`:

- read DB section and credentials path the same way `__main__.py` does;
- build `SportsHub`;
- wrap `hub.db.write` to count rows for the task;
- for `fty.*` tasks, call `hub.fty.connect_leagues()` first;
- resolve the dotted task name through `hub` and call it;
- special-case `util.sync_player_identity`: report its `minted` count;
- write `{"row_count": n}` to `SCS_RESULT_FILE` when set;
- print the traceback and exit 1 on failure.

Add `nba/database/run.py`:

- inline `update_schedule` query for `scs.nba.database`;
- call `scs_runner.run` with a `write_log` closure using `hub.db.write`;
- replace `__main__.py` as the Docker entrypoint;
- add `scs-runner` as a dependency (git source until it is published) and
  install it in the Docker image.

Keep the existing `cron.sh` lock/cadence/env wrapper. It already supplies
`UPDATE_LOG_SOURCE`, `UPDATE_LOG_KICKOFF`, and cadence.

## Mod `pts`

- Add its `update_schedule` row.
- Install `scs-runner` on the Pi (`pipx install` / `uv tool install` from the
  runner repo).
- Replace `cron_pts_mod.sh` with a thin wrapper:

```bash
#!/bin/bash
cd "$(dirname "$0")" || exit 1
exec scs-run daily
```

- Keep `Rscript modelling/mod_exec.R` as the command.
- Optional: have `mod_exec.R` write a row count to `SCS_RESULT_FILE`.
- The current `data/check_update_log.sql` uses stale columns
  (`process_date`, `table_name`, `successful_run`). Fix it separately or remove
  the check once the runner owns scheduling.

## League dash

Out of scope for the first pass. Keep `deploy/cron.sh` and
`dash_core/R/update_log.R` as they are. Optionally add a single row later:

```text
source      scs.nba.fty.league_dash
task        dashboard.sync
command     ./deploy/cron.sh daily
```

but note the nested `dashboard:*` rows it already writes.

## Tests

`scs_runner`:

- success path writes start, task, end rows;
- one failing task does not stop later tasks;
- timeout kills the command and records failure;
- `SCS_RESULT_FILE` row count is captured;
- log write failure is reported after the run;
- alert is called once with all failures;
- exit code is 1 when anything failed, including log write failures.

Integration:

- run the migration on postgres, compare row counts and order;
- run `python task.py nba.get_game_schedule` against postgres;
- run `scs-run --list` and one `--task` from `mod/pts`;
- verify `util.update_log` rows have the expected `source`, `cadence`,
  `job_kickoff`, `task`, `success`, `row_count`.

## Rollout order

1. Create `scs_runner` package and tests.
2. Add `database/task.py` and `database/run.py`; update Dockerfile.
3. Apply `util.update_schedule` migration on postgres; transport to Cockroach.
4. Update database rows to the generated commands and verify one manual run.
5. Add the mod `pts` row and wrapper; verify one manual run.
6. Update README/docs.
7. Leave league dash on its current path.
