"""Language-agnostic task execution for util.update_schedule rows."""

from scs_runner.alerts import (
    SmtpSettings,
    failure_alert,
    load_smtp_settings,
    send_failure_email,
)
from scs_runner.models import Failure, RunResult, Target, Task, TaskResult
from scs_runner.runner import run, run_sources

__all__ = [
    "Failure",
    "RunResult",
    "SmtpSettings",
    "Target",
    "Task",
    "TaskResult",
    "failure_alert",
    "load_smtp_settings",
    "run",
    "run_sources",
    "send_failure_email",
]
