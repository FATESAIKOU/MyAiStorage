"""AiStorage git-annex bundle 名稱與 GITMANIFEST 解析模組。

依據規格：docs/impl/group3-modules.md 第 3.1 節
- BundleName: GITBUNDLE-s<size>--<repo_uuid>-<sha256>
- Manifest: active（依序元組）、removed（frozenset）
- parse_manifest: 解析二進位 manifest 資料，空 active 或非法行拋出 MismatchError
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from aistorage.errors import MismatchError

_BUNDLE_NAME_PATTERN = re.compile(
    r"^GITBUNDLE-s(\d+)--([a-zA-Z0-9_-]+)-([0-9a-fA-F]{64})$"
)


@dataclass(frozen=True)
class BundleName:
    """Git annex bundle 檔名解析結構。"""

    name: str
    size: int
    repo_uuid: str
    sha256: str


def parse_bundle_name(name: str) -> BundleName | None:
    """解析 git-annex bundle 檔名。格式：GITBUNDLE-s<size>--<uuid>-<sha256>。

    若格式不符則回傳 None。
    """
    m = _BUNDLE_NAME_PATTERN.match(name)
    if not m:
        return None

    size_str, uuid_str, sha_str = m.groups()
    try:
        size = int(size_str)
    except ValueError:
        return None

    return BundleName(
        name=name,
        size=size,
        repo_uuid=uuid_str,
        sha256=sha_str.lower(),
    )


@dataclass(frozen=True)
class Manifest:
    """GITMANIFEST 解析結構。"""

    active: tuple[str, ...]
    removed: frozenset[str]


def parse_manifest(data: bytes) -> Manifest:
    """解析 GITMANIFEST 原始位元組。

    規則：
    - 每一行只能是 bundle 名稱或 '-' + bundle 名稱。
    - 空行略過；有任何未知或非法行格式時拋出 MismatchError。
    - active 清單若為空亦拋出 MismatchError。

    Returns:
        Manifest 實例。
    """
    if not isinstance(data, (bytes, bytearray)):
        raise MismatchError("manifest 資料必須是 bytes")

    try:
        text = data.decode("utf-8")
    except Exception as e:
        raise MismatchError(f"manifest 編碼非合法 UTF-8: {e}") from e

    active_list: list[str] = []
    removed_set: set[str] = set()

    for line_num, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith("-"):
            bundle_name = line[1:].strip()
            parsed = parse_bundle_name(bundle_name)
            if parsed is None:
                raise MismatchError(
                    f"manifest 第 {line_num} 行為無效之 removed bundle: '{line}'"
                )
            removed_set.add(parsed.name)
        else:
            bundle_name = line
            parsed = parse_bundle_name(bundle_name)
            if parsed is None:
                raise MismatchError(
                    f"manifest 第 {line_num} 行為無效之 active bundle: '{line}'"
                )
            active_list.append(parsed.name)

    if not active_list:
        raise MismatchError("manifest 中未包含任何 active bundle (active 清單為空)")

    return Manifest(
        active=tuple(active_list),
        removed=frozenset(removed_set),
    )
