"""AiStorage git-annex bundle 名稱與 GITMANIFEST 解析模組。

依據規格：
- docs/impl/group3-modules.md 第 3.1 節
- review-g3a.md M4、L（嚴格行檢查、僅限小寫 hex、repo_uuid 檢查、去重、無交集、normalize_refs）
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from aistorage.errors import MismatchError

# L: 嚴格限定 sha256 僅能為小寫 hex
_BUNDLE_NAME_PATTERN = re.compile(
    r"^GITBUNDLE-s(\d+)--([a-zA-Z0-9_-]+)-([0-9a-f]{64})$"
)

_NAMESPACE_PREFIX_PATTERN = re.compile(
    r"^refs/namespaces/git-remote-annex/([^/]+)/(.*)$"
)


@dataclass(frozen=True)
class BundleName:
    """Git annex bundle 檔名解析結構。"""

    name: str
    size: int
    repo_uuid: str
    sha256: str


def parse_bundle_name(name: str) -> BundleName | None:
    """解析 git-annex bundle 檔名。格式：GITBUNDLE-s<size>--<uuid>-<sha256>（限小寫 hex）。

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
        sha256=sha_str,
    )


@dataclass(frozen=True)
class Manifest:
    """GITMANIFEST 解析結構。"""

    active: tuple[str, ...]
    removed: frozenset[str]


def parse_manifest(data: bytes, *, repo_uuid: str | None = None) -> Manifest:
    """解析 GITMANIFEST 原始位元組。

    規則（M4 & L）：
    - 行尾只接受 \\n（或最後一行無 \\n）；前後帶空白視為非法行。
    - 每一行只能是 bundle 名稱或 '-' + bundle 名稱。
    - 若指定 repo_uuid，所有 bundle 之 repo_uuid 必須相符。
    - active 清單若為空、包含重複項目，或與 removed 清單存在交集，均拋出 MismatchError。

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
    active_set: set[str] = set()
    removed_set: set[str] = set()

    # 以 \n 切割，嚴格檢查每行格式
    lines = text.split("\n")
    for line_num, line in enumerate(lines, start=1):
        # 結尾可能有一個空行（標準文字檔），直接略過
        if line_num == len(lines) and line == "":
            continue
        if line == "":
            continue

        # L: 行前後不得有空白，且不接受 \r
        if line != line.strip() or "\r" in line:
            raise MismatchError(f"manifest 第 {line_num} 行格式不符（包含多餘空白或CR）: {repr(line)}")

        if line.startswith("-"):
            bundle_name = line[1:]
            parsed = parse_bundle_name(bundle_name)
            if parsed is None:
                raise MismatchError(
                    f"manifest 第 {line_num} 行為無效之 removed bundle: '{line}'"
                )
            if repo_uuid and parsed.repo_uuid != repo_uuid:
                raise MismatchError(
                    f"manifest 第 {line_num} 行 bundle 之 repo_uuid ({parsed.repo_uuid}) 不符預期 ({repo_uuid})"
                )
            removed_set.add(parsed.name)
        else:
            bundle_name = line
            parsed = parse_bundle_name(bundle_name)
            if parsed is None:
                raise MismatchError(
                    f"manifest 第 {line_num} 行為無效之 active bundle: '{line}'"
                )
            if repo_uuid and parsed.repo_uuid != repo_uuid:
                raise MismatchError(
                    f"manifest 第 {line_num} 行 bundle 之 repo_uuid ({parsed.repo_uuid}) 不符預期 ({repo_uuid})"
                )
            # M4: active 清單不可重複
            if parsed.name in active_set:
                raise MismatchError(f"manifest active 清單包含重複之 bundle: '{parsed.name}'")
            active_set.add(parsed.name)
            active_list.append(parsed.name)

    if not active_list:
        raise MismatchError("manifest 中未包含任何 active bundle (active 清單為空)")

    # M4: active 與 removed 不可有交集
    overlap = active_set & removed_set
    if overlap:
        raise MismatchError(f"manifest active 與 removed 清單存在交集: {overlap}")

    return Manifest(
        active=tuple(active_list),
        removed=frozenset(removed_set),
    )


def normalize_refs(raw: dict[str, str], *, repo_uuid: str | None = None) -> dict[str, str]:
    """正規化遠端 ls-remote 或 bundle list-heads 宣告之 ref 映射表。

    規則（M4）：
    - 一律排除 HEAD 與 peeled ref（以 ^{} 結尾者）。
    - 若指定 repo_uuid，只接受 refs/namespaces/git-remote-annex/<repo_uuid>/<clean_ref> 開頭者，其餘拋出 MismatchError。
    - 若未指定 repo_uuid，將 refs/namespaces/git-remote-annex/<任意uuid>/ 前綴剝除。
    - 剝除前綴後若有同名衝突，拋出 MismatchError。

    Returns:
        clean_ref -> sha 字典。
    """
    normalized: dict[str, str] = {}

    for ref_name, sha in raw.items():
        # 排除 HEAD 與 peeled ref
        if ref_name == "HEAD" or ref_name.endswith("^{}"):
            continue

        m = _NAMESPACE_PREFIX_PATTERN.match(ref_name)
        if m:
            u, clean_ref = m.group(1), m.group(2)
            if repo_uuid and u != repo_uuid:
                raise MismatchError(
                    f"ref '{ref_name}' 之 namespace uuid ({u}) 不符合預期 ({repo_uuid})"
                )
        else:
            if repo_uuid:
                raise MismatchError(
                    f"ref '{ref_name}' 缺少預期之 namespace 前綴 (refs/namespaces/git-remote-annex/{repo_uuid}/)"
                )
            clean_ref = ref_name

        if clean_ref in normalized and normalized[clean_ref] != sha:
            raise MismatchError(
                f"ref 正規化衝突: '{clean_ref}' 同時映射至多個 sha ({normalized[clean_ref]}, {sha})"
            )
        normalized[clean_ref] = sha

    return normalized
