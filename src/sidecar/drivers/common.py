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


import json


def extract_secrets(env: dict[str, str]) -> tuple[str, ...]:
    """Extract real secret values (keys / tokens) from env (D6, M3).

    Excludes switch flags ('1', '0'), model names, endpoint URLs,
    and whole configuration blobs.
    """
    secrets: list[str] = []
    for key, val in env.items():
        if not val or not isinstance(val, str):
            continue
        if key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN") or key.upper().endswith(
            ("_API_KEY", "_AUTH_TOKEN", "_OAUTH_TOKEN", "_SECRET", "_KEY")
        ):
            secrets.append(val)
        elif key == "OPENCODE_CONFIG_CONTENT":
            try:
                data = json.loads(val)
                if isinstance(data, dict):
                    providers = data.get("provider", {})
                    if isinstance(providers, dict):
                        for prov in providers.values():
                            if isinstance(prov, dict):
                                opts = prov.get("options", {})
                                if isinstance(opts, dict):
                                    api_key = opts.get("apiKey")
                                    if isinstance(api_key, str) and api_key:
                                        secrets.append(api_key)
            except Exception:
                pass
    return tuple(dict.fromkeys(secrets))


def mask_secrets(text: str, secrets: Sequence[str]) -> str:
    """Replace real secrets with `<masked>` (D6, M3)."""
    for secret in secrets:
        if secret and len(secret) >= 4:
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

