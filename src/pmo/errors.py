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


class IdentityMismatch(PMOError):
    """The identity guard: a sending command runs as another seat.

    Every command that sent letter checks `ANGAREION_IDENTITY` against the
    settings' PMO identity first and refuses otherwise (design §3.2「送信前的
    身分守門」; the current tool has the same gate). Refusal is exit 2.
    """


class UsageError(PMOError):
    """The arguments name something that does not exist (exit 1).

    E.g. an unknown work ID or an unsupported report status: the command
    understood what the caller wants but cannot do it (design §3.2's exit 1).
    """


class SentButNotRecorded(PMOError):
    """The letter went out but the state file could not be updated.

    The design's exit 2 means「送不出去」; a send followed by a state-file
    failure is the one case where the letter *did* go out. The caller must say
    so explicitly (message id included) instead of letting the caller retry and
    send twice: re-sending an ack or a report is visible to the secretary.
    `message_id` is the id the back end returned for the letter.
    """

    def __init__(self, message: str, message_id: str = "") -> None:
        super().__init__(message)
        self.message_id = message_id
