"""AiStorage GITMANIFEST 與 bundle 檔案格式解析模組。

依據規格：
- docs/impl/group3-modules.md 第 1.1 節
- review-g3a.md M4（嚴格 hex 小寫、去除空白 CR、repo_uuid 驗證、無重複無交集）
- review-g3a-recheck.md N1（normalize_bundle_heads、normalize_ls_remote 雙入口；repo_uuid 必填）
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from aistorage.errors import MismatchError

_BUNDLE_NAME_PATTERN = re.compile(
    r"^GITBUNDLE-s(?P<size>\d+)--(?P<repo_uuid>[0-9a-f-]+)-(?P<sha256>[0-9a-f]{64})$"
)
_NAMESPACE_PREFIX_PATTERN = re.compile(
    r"^refs/namespaces/git-remote-annex/([0-9a-f-]+)/(.*)$"
)


@dataclass(frozen=True)
class BundleName:
    """GITBUNDLE 檔名解析結構。"""

    name: str
    size: int
    repo_uuid: str
    sha256: str


def parse_bundle_name(name: str) -> BundleName | None:
    """解析 bundle 檔名。若符合 GITBUNDLE 格式回傳 BundleName，否則回傳 None。"""
    m = _BUNDLE_NAME_PATTERN.match(name)
    if not m:
        return None
    return BundleName(
        name=name,
        size=int(m.group("size")),
        repo_uuid=m.group("repo_uuid"),
        sha256=m.group("sha256").lower(),
    )


@dataclass(frozen=True)
class Manifest:
    """GITMANIFEST 解析結構。"""

    active: tuple[str, ...]
    removed: frozenset[str]


def parse_manifest(data: bytes, *, repo_uuid: str) -> Manifest:
    """解析 GITMANIFEST 原始位元組。

    規則（M4, L & N1）：
    - 行尾只接受 \\n（或最後一行無 \\n）；前後帶空白視為非法行，不接受 \\r。
    - 每一行只能是 bundle 名稱或 '-' + bundle 名稱。
    - 所有 bundle 之 repo_uuid 必須與傳入之 repo_uuid 完全相符（必填參數）。
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
            if parsed.repo_uuid != repo_uuid:
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
            if parsed.repo_uuid != repo_uuid:
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


def normalize_bundle_heads(raw: dict[str, str], *, repo_uuid: str) -> dict[str, str]:
    """正規化 bundle list-heads 宣告之 ref 映射表。

    規則（N1）：
    - 一律排除 HEAD 與 peeled ref（以 ^{} 結尾者）。
    - 嚴格要求每個 ref 均以 refs/namespaces/git-remote-annex/<repo_uuid>/ 開頭。
    - 剝除該前綴，回傳 clean_ref -> sha 字典。
    - 若 ref 未包含該前綴或 uuid 不符，拋出 MismatchError。
    - 剝除前綴後若有同名衝突，拋出 MismatchError。
    """
    normalized: dict[str, str] = {}

    for ref_name, sha in raw.items():
        if ref_name == "HEAD" or ref_name.endswith("^{}"):
            continue

        m = _NAMESPACE_PREFIX_PATTERN.match(ref_name)
        if not m:
            raise MismatchError(
                f"bundle head '{ref_name}' 缺少預期之 namespace 前綴 (refs/namespaces/git-remote-annex/{repo_uuid}/)"
            )
        u, clean_ref = m.group(1), m.group(2)
        if u != repo_uuid:
            raise MismatchError(
                f"bundle head '{ref_name}' 之 namespace uuid ({u}) 不符合預期 ({repo_uuid})"
            )

        if clean_ref in normalized and normalized[clean_ref] != sha:
            raise MismatchError(
                f"ref 正規化衝突: '{clean_ref}' 同時映射至多個 sha ({normalized[clean_ref]}, {sha})"
            )
        normalized[clean_ref] = sha

    return normalized


def normalize_ls_remote(raw: dict[str, str]) -> dict[str, str]:
    """正規化 git ls-remote 回傳之 ref 映射表。

    規則（N1）：
    - 一律排除 HEAD 與 peeled ref（以 ^{} 結尾者）。
    - 嚴格要求 ref 為標準 refs/ 開頭且「不得」包含 namespace 前綴。
    - 若包含 refs/namespaces/ 拋出 MismatchError。
    - 若有衝突拋出 MismatchError。
    """
    normalized: dict[str, str] = {}

    for ref_name, sha in raw.items():
        if ref_name == "HEAD" or ref_name.endswith("^{}"):
            continue

        if ref_name.startswith("refs/namespaces/"):
            raise MismatchError(
                f"ls-remote ref 不得包含 namespace 前綴: '{ref_name}'"
            )
        if not ref_name.startswith("refs/"):
            raise MismatchError(f"無效之 ref 格式: '{ref_name}'")

        if ref_name in normalized and normalized[ref_name] != sha:
            raise MismatchError(
                f"ref 正規化衝突: '{ref_name}' 同時映射至多個 sha ({normalized[ref_name]}, {sha})"
            )
        normalized[ref_name] = sha

    return normalized


def normalize_refs(raw: dict[str, str], *, repo_uuid: str | None = None) -> dict[str, str]:
    """相容性轉接函式：依據是否具備 namespace 前綴自動分流。"""
    has_ns = any(k.startswith("refs/namespaces/") for k in raw if k != "HEAD" and not k.endswith("^{}"))
    if has_ns:
        if not repo_uuid:
            raise MismatchError("帶有 namespace 之 ref 正規化必須提供 repo_uuid")
        return normalize_bundle_heads(raw, repo_uuid=repo_uuid)
    return normalize_ls_remote(raw)
