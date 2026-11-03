"""Where the pmo settings and state live: environment only, never guessed.

`PMO_SKILL_CONFIG` names the teams settings file and `PMO_SKILL_STATE_DIR` the
state directory (design §3.4). Neither is set → refuse to run; no default path
is ever assumed. Callers map `MissingEnvError` onto their exit codes.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import MissingEnvError

#: The environment variable that names the teams settings file.
CONFIG_ENV = "PMO_SKILL_CONFIG"

#: The environment variable that names the state directory.
STATE_DIR_ENV = "PMO_SKILL_STATE_DIR"


def get_config_path() -> Path:
    """The teams settings file, exactly as the environment names it."""
    value = os.environ.get(CONFIG_ENV)
    if not value:
        raise MissingEnvError(f"{CONFIG_ENV} is not set; refusing to use a default path")
    return Path(value)


def get_state_dir() -> Path:
    """The state directory, exactly as the environment names it."""
    value = os.environ.get(STATE_DIR_ENV)
    if not value:
        raise MissingEnvError(f"{STATE_DIR_ENV} is not set; refusing to use a default path")
    return Path(value)
