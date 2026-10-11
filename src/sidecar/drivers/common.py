"""What the two drivers share: the model choice from a subscription.

D6: never let the agent pick its own default - it may hit an image-preview
model or the public Zen models.  Sources, in order: `models.default`, the
first `models.allow` entry without a wildcard; none means refuse (`no_model`,
open answers 4xx).
"""

from __future__ import annotations

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
