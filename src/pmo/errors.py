"""Errors raised by the `pmo` package.

The family is `ValueError` based, the way `angareion` uses `ConfigError(ValueError)`.
"""

from __future__ import annotations


class PMOError(ValueError):
    """Base class for the pmo errors."""


class MissingEnvError(PMOError):
    """A required environment variable is not set.

    The settings and state locations are given by `PMO_SKILL_CONFIG` and
    `PMO_SKILL_STATE_DIR` only (design §3.4): missing means refuse to run —
    never a guessed default path.
    """


class SchemaError(PMOError):
    """A settings or state file does not match its schema (bad file → exit 2)."""
