"""Credentials-file helpers for alerting."""

from __future__ import annotations

import configparser
import os
from pathlib import Path

ENV_CREDENTIALS_INI = "SCS_HUB_CREDENTIALS"


def credentials_path(explicit: str | Path | None = None) -> Path:
    """Resolve the credentials file: explicit > env > ~/.config default."""
    if explicit:
        return Path(explicit).expanduser()
    from_env = os.environ.get(ENV_CREDENTIALS_INI)
    if from_env:
        return Path(from_env).expanduser()
    return Path.home() / ".config" / "scs_hub_credentials.ini"


def read_section(section: str, path: str | Path | None = None) -> dict[str, str]:
    """Return one credentials section, with a clear error when it is missing."""
    ini = credentials_path(path)
    if not ini.exists():
        raise FileNotFoundError(f"Credentials file not found at {ini}")
    parser = configparser.ConfigParser()
    parser.read(ini)
    if not parser.has_section(section):
        raise ValueError(f"No [{section}] section in {ini}")
    return dict(parser.items(section))
