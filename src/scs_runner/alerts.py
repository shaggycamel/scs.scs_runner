"""SMTP failure alerts using the shared credentials INI."""

from __future__ import annotations

import logging
import os
import smtplib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from scs_runner.config import read_section
from scs_runner.models import Failure

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmtpSettings:
    user: str
    password: str
    recipient: str
    host: str = "smtp.gmail.com"
    port: int = 587


def load_smtp_settings(
    ini_path: str | Path | None = None,
    *,
    env: dict[str, str] | None = None,
) -> SmtpSettings | None:
    """Load ``[smtp]`` settings; env vars win when present.

    Returns ``None`` when user/password are not configured, which means the
    run continues without sending mail.
    """
    values = os.environ if env is None else env
    user = values.get("SMTP_USER")
    password = values.get("SMTP_PASSWORD")
    recipient = values.get("ALERT_TO")
    host = values.get("SMTP_HOST") or "smtp.gmail.com"
    port = _int(values.get("SMTP_PORT"), 587)

    try:
        section = read_section("smtp", ini_path)
    except (FileNotFoundError, ValueError):
        section = {}

    user = user or section.get("user")
    password = password or section.get("password")
    recipient = recipient or section.get("to")
    host = section.get("host") or host
    port = _int(section.get("port"), port)

    if not (user and password):
        return None
    return SmtpSettings(
        user=user,
        password=password,
        recipient=recipient or user,
        host=host,
        port=port,
    )


def send_failure_email(
    failures: Sequence[Failure],
    *,
    source: str,
    settings: SmtpSettings | None = None,
    ini_path: str | Path | None = None,
) -> bool:
    """Send one failure summary. Best-effort: errors are logged, not raised."""
    settings = settings or load_smtp_settings(ini_path)
    if settings is None:
        logger.warning("no smtp user/password configured; skipping failure email")
        return False

    body = f"source: {source}\n\n"
    for failure in failures:
        body += f"{failure.task}\n{failure.error}\n\n"

    message = (
        f"From: {settings.user}\n"
        f"To: {settings.recipient}\n"
        f"Subject: scs-runner failure: {source}\n\n"
        f"{body}"
    )

    try:
        with smtplib.SMTP(settings.host, settings.port, timeout=30) as server:
            server.starttls()
            server.login(settings.user, settings.password)
            server.sendmail(settings.user, settings.recipient, message)
    except Exception:
        logger.exception("could not send failure email")
        return False
    return True


def failure_alert(
    source: str,
    *,
    ini_path: str | Path | None = None,
    settings: SmtpSettings | None = None,
) -> Callable[[Sequence[Failure]], None]:
    """Build the callback shape expected by ``scs_runner.run``."""

    def alert(failures: Sequence[Failure]) -> None:
        send_failure_email(failures, source=source, settings=settings, ini_path=ini_path)

    return alert


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
