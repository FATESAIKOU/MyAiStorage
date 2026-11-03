"""sanitize: printed strings never carry a token or a key (design §3.2, D-12).

Every string the pmo command prints goes through `sanitize`; error messages
additionally never contain token, key or message text — that is on the caller
(D-12), `sanitize` only redacts credential-shaped values.

The redaction shapes are the token-shaped runs `agora.credscan` scans for, plus
the same `password`/`client_secret`/`refresh_token`/`access_token` value shape
(a message body printed as `[msg]`全文 can carry one). The pattern list is not
exhaustive by design: passing exact values as `secrets` hides those as well.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping

#: What a redacted value is replaced with.
REDACTED = "***"

_TOKEN = r"[A-Za-z0-9_-]"

#: Credential-shaped runs to redact even when the exact value is not known.
TOKEN_SHAPES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        rf"ya29\.{_TOKEN}{{20,}}",
        rf"1//0{_TOKEN}{{20,}}",
        rf"GOCSPX-{_TOKEN}{{20,}}",
        r"gh[pous]_[A-Za-z0-9]{30,}",
        r"github_pat_[A-Za-z0-9_]{30,}",
        rf"sk-ant-{_TOKEN}{{20,}}",
        rf"sk-{_TOKEN}{{20,}}",
        r"AKIA[0-9A-Z]{16}",
        r"xox[bap]-[A-Za-z0-9-]{10,}",
        rf"AIza{_TOKEN}{{35}}",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    )
)

#: A secret named by its key (`password: …`), the one credscan shape whose value
#: is redacted rather than the whole run: the key says what was hidden.
SECRET_FIELD: re.Pattern[str] = re.compile(
    r"""(?ix)(?<![A-Za-z0-9])(?:password|client_secret|refresh_token|access_token)(?![A-Za-z0-9_])
    \\?["']?\s*[:=]\s*\\?["']?([^\s"'\\,}{]{8,})"""
)


def sanitize(text: str, secrets: Iterable[str] = ()) -> str:
    """Replace each given secret and every credential-shaped run with ***.

    Normal text (event lines, AI names, channels, ULIDs, addresses) is kept.
    """
    # Longest first: a short secret that is the front of a longer one would
    # otherwise leave the rest of the longer one in the output (review 中-2).
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    for pattern in TOKEN_SHAPES:
        text = pattern.sub(REDACTED, text)
    return SECRET_FIELD.sub(lambda match: match.group(0).replace(match.group(1), REDACTED), text)


def known_fields_only(record: Mapping[str, object], known: Iterable[str]) -> dict[str, object]:
    """Only the known fields of a record reach the printer (D-12: e.g. sidecar).

    Unknown fields are dropped on the way out (they may still exist on the
    record itself; state files preserve them).
    """
    keep = set(known)
    return {key: value for key, value in record.items() if key in keep}


def sanitize_fields(value: object) -> object:
    """A copy of `value` with every string in it past `sanitize()` (D-12).

    The state files carry message text verbatim (`title`, `spec`, `notes`, a
    `[msg]` body), and the sidecar's `exit.reason` sits nested in a team state:
    one recursive pass keeps both files free of credential-shaped runs before
    they touch the disk. Both state stores (the inbox ledger and the team
    state) call this, so there is one redaction rule, not two copies.
    """
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, list):
        return [sanitize_fields(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_fields(item) for key, item in value.items()}
    return value
