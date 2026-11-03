"""One JSON file read/written the same way by every pmo state file.

Writes go through a same-directory temporary file and `os.replace` (design
§3.4: atomic write). The `tick.json.lock` flock orchestration of read-modify-
write rounds is tick's own machinery (§3.4, T-5) and is not here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .errors import SchemaError


def read_json(path: Path) -> object:
    """Read one JSON file; invalid JSON is a bad file, i.e. a SchemaError."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise SchemaError(f"{path}: not valid JSON ({error.msg} at line {error.lineno})") from error
    if not isinstance(data, dict):
        raise SchemaError(f"{path}: the file does not hold one JSON object")
    return data


def write_json(path: Path, data: object, *, sort_keys: bool = False) -> None:
    """Write one JSON object atomically (temporary file in the same directory).

    The temporary name carries the pid, so two writers of the same file cannot
    write into one another's buffer; every pmo state file goes through this one
    function (design §3.4: 同目錄暫存檔＋`os.replace`).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=sort_keys) + "\n"
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, path)
    except OSError as error:
        temporary.unlink(missing_ok=True)  # 失敗時不留半截的暫存檔
        raise SchemaError(f"state 檔寫不起來 {path}：{error}") from error
