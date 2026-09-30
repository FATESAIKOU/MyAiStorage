"""AiStorage phase 1 core package."""


from aistorage.inbox import (
    check_raw,
    is_complete,
    sign_sidecar_bytes,
    validate_sidecar,
    verify_sidecar_bytes,
)
from aistorage.reading import (
    check_continuation,
    messages_before,
    plain_text,
    validate_reading,
)
from aistorage.schema import (
    FieldError,
    classify_id,
    make_item_id,
    make_session_id,
    strip_claimed_producer,
    validate_inbox_metadata,
    validate_record_metadata,
)

__all__ = [
    "FieldError",
    "validate_inbox_metadata",
    "validate_record_metadata",
    "strip_claimed_producer",
    "make_session_id",
    "make_item_id",
    "classify_id",
    "validate_reading",
    "check_continuation",
    "messages_before",
    "plain_text",
    "sign_sidecar_bytes",
    "verify_sidecar_bytes",
    "validate_sidecar",
    "check_raw",
    "is_complete",
    "generate_keypair",
    "validate_registry",
    "load_registry",
    "Registry",
]


def __getattr__(name: str):
    if name in ("Registry", "generate_keypair", "load_registry", "validate_registry"):
        import aistorage.identity as _id

        return getattr(_id, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


