"""AiStorage 來源轉換器套件。

依據規格：docs/impl/group3-modules.md 第 5 節
"""

from aistorage.converters.base import ConversionError, Converter, SessionFacts
from aistorage.converters.opencode import OpencodeConverter

CONVERTERS: dict[str, Converter] = {
    "opencode": OpencodeConverter(),
}


def get_converter(source: str) -> Converter:
    """取得指定來源應用之轉換器實例。若不支援拋出 KeyError。"""
    if source not in CONVERTERS:
        raise KeyError(f"未註冊之來源轉換器: '{source}'")
    return CONVERTERS[source]


__all__ = [
    "ConversionError",
    "Converter",
    "SessionFacts",
    "OpencodeConverter",
    "CONVERTERS",
    "get_converter",
]
