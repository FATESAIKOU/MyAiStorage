"""What the two drivers share: the model choice from a subscription.

D6: never let the agent pick its own default - it may hit an image-preview
model or the public Zen models.  Sources, in order: `models.default`, the
first `models.allow` entry without a wildcard; none means refuse (`no_model`,
open answers 4xx).
"""

from __future__ import annotations

from typing import Any, Sequence

from ..protocols import NoModelError, Subscription


def pick_model(subscription: Subscription) -> str:
    models = subscription.models or {}
    default = models.get("default")
    if isinstance(default, str) and default:
        return default
    for allowed in models.get("allow") or []:
        if isinstance(allowed, str) and allowed and "*" not in allowed:
            return allowed
    raise NoModelError(
        f"subscription {subscription.id!r} names no usable model "
        "(models.default or a concrete models.allow entry)"
    )


def mask_secrets(text: str, secrets: Sequence[str]) -> str:
    """Replace every value `env_for` produced with `<masked>` (D6)."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, "<masked>")
    return text


def stderr_excerpt(
    proc: Any, tail_lines: Sequence[str], secrets: Sequence[str], limit: int = 10
) -> str:
    """A suffix for driver errors: the child's stderr tail, keys masked.

    Store-spawned processes keep their own tail (`AgoraProcess.stderr_tail`);
    directly spawned ones are drained into the handle's bounded deque.
    """
    getter = getattr(proc, "stderr_tail", None)
    if callable(getter):
        try:
            tail = getter()
        except Exception:
            tail = ""
    else:
        tail = "\n".join(list(tail_lines)[-limit:])
    tail = mask_secrets((tail or "").strip(), secrets)
    return f"; stderr tail: {tail}" if tail else ""

