"""讀 GitHub Actions workflow 的極小 YAML 讀取器（只給測試用）。

為什麼不用 PyYAML：專案沒有 YAML 相依（`pyproject.toml` 只有 jsonschema 與
cryptography），而 workflow 檔是受版控的固定檔案——為它加一個執行期相依不划算，
安裝 actionlint 也不符合本組的做法（不 brew install）。所以這裡自帶一個**只支援
workflow 會用到的子集**的讀取器：

- block mapping（`key:` ＋ 縮排的子節點）
- block sequence（`- ...`）
- 純量：雙引號／單引號字串、單引號外的一般字串、`{}`、`[]`、`true`／`false`、整數
- 區塊純量 `|`（`run: |` 用的就是它，內容原樣保留）

**不支援**（出現時直接丟錯，不要默默猜）：anchor／alias（`*x`）、tag（`!x`）、
多行摺疊純量（`>`）、文件分隔符（`---`）、同一個 key 出現兩次。測試檔一旦用到
這些，這裡會立刻爆，而不是讓斷言 quietly 地看錯東西。

用法：

```python
doc = load_workflow(Path(".github/workflows/committer.yml"))
assert doc["on"]["schedule"] == [{"cron": "7 */6 * * *"}]
```

`load_workflow` 在 PyYAML 可用時會**順便交叉比對**兩份解析結果，所以這個讀取器
一旦與真正的 YAML 語意分歧，測試就會失敗（見 test_workflow_committer_yml.py）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["load_workflow", "load_yaml", "YamlSubsetError"]


class YamlSubsetError(AssertionError):
    """檔案用到這個讀取器不支援的 YAML 語法（fail-closed，不要默默略過）。"""


_UNSUPPORTED_PREFIX = ("&", "*", "!", "---", "...")


def _strip_comment(line: str) -> str:
    """去掉註解，但不去掉引號裡的 `#`（workflow 的 `run: |` 區塊不走這裡）。"""
    quote: str | None = None
    for i, ch in enumerate(line):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            return line[:i]
    return line


def _scalar(raw: str) -> Any:
    text = raw.strip()
    if text == "{}":
        return {}
    if text == "[]":
        return []
    if text.startswith('"') and text.endswith('"') and len(text) >= 2:
        return text[1:-1]
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1].replace("''", "'")
    if text in ("true", "True"):
        return True
    if text in ("false", "False"):
        return False
    if text in ("null", "~", ""):
        return None
    try:
        return int(text)
    except ValueError:
        return text


def _split_key(content: str) -> tuple[str, str] | None:
    """`key: value` → (key, value)；不是 key 行就回傳 None。"""
    quote: str | None = None
    for i, ch in enumerate(content):
        if quote is not None:
            if ch == quote:
                quote = None
            continue
        if ch in "\"'":
            quote = ch
        elif ch == ":" and (i + 1 == len(content) or content[i + 1] in " \t"):
            key = content[:i].strip()
            if key.startswith(('"', "'")) and key.endswith(('"', "'")) and len(key) >= 2:
                key = key[1:-1]
            return key, content[i + 1:].strip()
    return None


class _Reader:
    def __init__(self, text: str) -> None:
        # (indent, content, lineno)：縮排與內容只看非空行；block scalar 另外
        # 回頭從 `self.text_lines` 取**原始**行（裡面有縮排、有空行、有 # 註解，
        # 對 `run: |` 來說全部都是內容的一部分）。
        self.text_lines = text.splitlines()
        self.lines: list[tuple[int, str, int]] = []
        for lineno, raw in enumerate(self.text_lines, start=1):
            if raw.strip() in ("---", "..."):
                raise YamlSubsetError(f"第 {lineno} 行：文件分隔符不支援")
            stripped = _strip_comment(raw).rstrip()
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            content = stripped.strip()
            if content.startswith(_UNSUPPORTED_PREFIX):
                raise YamlSubsetError(f"第 {lineno} 行：{content!r}（anchor/tag/分隔符不支援）")
            if "\t" in raw[:indent]:
                raise YamlSubsetError(f"第 {lineno} 行：縮排含 tab")
            self.lines.append((indent, content, lineno))
        self.pos = 0

    # -- 基本操作 ---------------------------------------------------------
    def peek(self) -> tuple[int, str, int] | None:
        return self.lines[self.pos] if self.pos < len(self.lines) else None

    def parse_block(self, indent: int) -> Any:
        head = self.peek()
        if head is None:
            return None
        if head[1].startswith("- "):
            return self.parse_sequence(indent)
        return self.parse_mapping(indent)

    def parse_sequence(self, indent: int) -> list[Any]:
        out: list[Any] = []
        while True:
            head = self.peek()
            if head is None or head[0] != indent or not head[1].startswith("- "):
                break
            _, content, lineno = head
            rest = content[2:].strip()
            self.pos += 1
            if not rest:
                out.append(self.parse_block(indent + 2))
                continue
            pair = _split_key(rest)
            if pair is None:
                out.append(_scalar(rest))
                continue
            # `- key: value` 是「以 key 開頭的 mapping」，後續縮排更深
            key, value = pair
            child_indent = indent + 2 + (len(content[2:]) - len(content[2:].lstrip()))
            item: dict[str, Any] = {}
            self._assign(item, key, value, child_indent, lineno)
            out.append(self.parse_mapping(child_indent, into=item, allow_empty=True))
        return out

    def parse_mapping(self, indent: int, *, into: dict[str, Any] | None = None,
                      allow_empty: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {} if into is None else into
        while True:
            head = self.peek()
            if head is None or head[0] != indent:
                break
            cur_indent, content, lineno = head
            if content.startswith("- "):
                break
            pair = _split_key(content)
            if pair is None:
                raise YamlSubsetError(f"第 {lineno} 行：不是 key 行：{content!r}")
            key, value = pair
            if key in out:
                raise YamlSubsetError(f"第 {lineno} 行：重複的 key {key!r}")
            self.pos += 1
            self._assign(out, key, value, cur_indent, lineno)
        if not out and not allow_empty:
            raise YamlSubsetError(f"縮排 {indent} 底下沒有 mapping")
        return out

    def _assign(self, out: dict[str, Any], key: str, value: str,
                indent: int, lineno: int) -> None:
        if value in ("|", ">", "|-", ">-", "|+", ">+"):
            if value.startswith(">"):
                raise YamlSubsetError(f"第 {lineno} 行：摺疊純量 {value!r} 不支援")
            out[key] = self._read_block_scalar(indent, lineno)
            return
        if value:
            out[key] = _scalar(value)
            return
        nxt = self.peek()
        if nxt is not None and nxt[0] > indent:
            out[key] = self.parse_block(nxt[0])
        elif nxt is not None and nxt[0] == indent and nxt[1].startswith("- "):
            out[key] = self.parse_sequence(indent)
        else:
            out[key] = None

    def _read_block_scalar(self, indent: int, key_lineno: int) -> str:
        """`run: |` 的內容：去掉區塊共同縮排，保留相對縮排、空行與 `#` 註解。"""
        start = self.pos
        base: int | None = None
        end_lineno = key_lineno
        while True:
            head = self.peek()
            if head is None or head[0] <= indent:
                break
            if base is None and head[1]:
                base = head[0]
            end_lineno = head[2]
            self.pos += 1
        if base is None:
            return ""
        body = [raw[base:] if raw[:base].isspace() or len(raw) <= base else raw.lstrip(" ")
                for raw in self.text_lines[key_lineno:end_lineno]]
        while body and not body[-1].strip():
            body.pop()
        return "\n".join(body) + "\n"


def load_yaml(text: str) -> Any:
    reader = _Reader(text)
    if reader.peek() is None:
        return None
    doc = reader.parse_block(reader.peek()[0])
    if reader.peek() is not None:
        _, content, lineno = reader.peek()  # type: ignore[misc]
        raise YamlSubsetError(f"第 {lineno} 行之後還有內容，解析不了：{content!r}")
    return doc


def _normalize_on_key(doc: Any) -> Any:
    """YAML 1.1 的 `on` 會被 PyYAML 讀成布林 true；GitHub 自己讀的是字串 `on`。"""
    if not isinstance(doc, dict):
        return doc
    out: dict[Any, Any] = {}
    for key, value in doc.items():
        out["on" if key is True else key] = _normalize_on_key(value)
    return out


def load_workflow(path: Path | str) -> dict[str, Any]:
    """讀一個 workflow 檔（路徑）。PyYAML 在場時交叉比對兩份解析結果。"""
    text = Path(path).read_text(encoding="utf-8")
    doc = load_yaml(text)
    if not isinstance(doc, dict):
        raise YamlSubsetError(f"{path} 解析出來不是 mapping")
    try:
        import yaml  # type: ignore
    except ImportError:
        return doc
    reference = _normalize_on_key(yaml.safe_load(text))
    if reference != doc:
        raise YamlSubsetError(
            f"{path} 的解析結果與 PyYAML 不一致（自帶讀取器不可信）\n"
            f"自帶：{doc!r}\nPyYAML：{reference!r}")
    return doc
