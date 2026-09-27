"""opencode 的 HTTP／CLI 存取層（tasks 5.2）。

依據：docs/impl/group5-7-modules.md 第 2.1 節、design D4、
技術驗證 1.7a（匯出非互動）／1.7b（匯出確定）／1.7e（`time.archived` 是明確訊號）／
1.7h（`GET /session` 含子 Session）。

重點（都是實測過的）：
- `GET /session?directory=<dir>` 是**全域清單，包含子 Session**（1.7h H1）；
  CLI 的 `opencode session list` **不含**子 Session，也**不輸出** `time.archived`，
  所以同步器一律走 API。
- `opencode export <id>` 把單一 JSON 物件寫到 stdout（位元組原樣、確定）；
  **一定要明示 id**（不帶 id 會進入互動選單卡住，1.7a）。
- 停止只認 `time.archived > 0`（明確操作，不是閒置推測）。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request

from aistorage.errors import ReadError

DEFAULT_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_DIRECTORY = "/work"
REQUEST_TIMEOUT_S = 30.0
EXPORT_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class OcSession:
    """opencode 的一個 Session（主或子）。"""

    id: str
    parent_id: str | None = None
    title: str | None = None
    updated_ms: int = 0
    archived_ms: int | None = None

    @property
    def is_main(self) -> bool:
        """是否為主 Session（沒有母 Session）。"""
        return not self.parent_id

    def session_id(self, source: str = "opencode") -> str:
        """Agora 的 Session id（`<source>:<source_session_id>`）。"""
        return f"{source}:{self.id}"

    def parent_session_id(self, source: str = "opencode") -> str | None:
        """母 Session 的 Agora id；主 Session 回 None。"""
        return f"{source}:{self.parent_id}" if self.parent_id else None


def _as_ms(value: Any) -> int | None:
    """把 opencode 的時間欄位轉成毫秒整數；0 與 None 都視為「沒有時間」。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        ms = int(value)
    elif isinstance(value, str) and value.strip():
        try:
            ms = int(float(value))
        except ValueError:
            return None
    else:
        return None
    return ms if ms > 0 else None


def _parse_session(data: Any) -> OcSession:
    """把 API 的一筆 session 物件轉成 OcSession（容忍缺欄位）。"""
    if not isinstance(data, dict):
        raise ReadError("opencode API 回傳的 session 不是物件")
    sid = data.get("id")
    if not isinstance(sid, str) or not sid:
        raise ReadError("opencode API 回傳的 session 缺少 id")
    time_info = data.get("time") if isinstance(data.get("time"), dict) else {}
    parent = data.get("parentID") or data.get("parent_id")
    return OcSession(
        id=sid,
        parent_id=parent if isinstance(parent, str) and parent else None,
        title=data.get("title") if isinstance(data.get("title"), str) else None,
        updated_ms=_as_ms(time_info.get("updated")) or 0,
        archived_ms=_as_ms(time_info.get("archived")),
    )


class OpencodeApi:
    """opencode `serve` 的 HTTP API ＋ `opencode export` CLI。"""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        directory: str = DEFAULT_DIRECTORY,
        opencode_bin: str = "opencode",
        timeout: float = REQUEST_TIMEOUT_S,
        export_timeout: float = EXPORT_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.directory = directory
        self.opencode_bin = opencode_bin
        self.timeout = timeout
        self.export_timeout = export_timeout

    # ── HTTP ────────────────────────────────────────────────────────────

    def _url(self, path: str) -> str:
        query = urllib.parse.urlencode({"directory": self.directory})
        return f"{self.base_url}{path}?{query}"

    def _request(
        self, path: str, *, method: str = "GET", payload: dict | None = None
    ) -> Any:
        url = self._url(path)
        data = None
        headers = {"accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["content-type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            raise ReadError(f"opencode API 失敗 (HTTP {e.code}): {method} {path}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ReadError(f"連不上 opencode API（{self.base_url}）：{type(e).__name__}") from None
        if not body:
            return None
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ReadError(f"opencode API 回應不是合法 JSON: {e}") from None

    def list_sessions(self) -> list[OcSession]:
        """全域清單（**包含子 Session**，1.7h H1），依 id 排序。"""
        data = self._request("/session")
        if isinstance(data, dict):
            data = data.get("sessions") or data.get("data") or []
        if not isinstance(data, list):
            raise ReadError("opencode API /session 的回應不是清單")
        out = [_parse_session(item) for item in data]
        return sorted(out, key=lambda s: s.id)

    def children(self, session_id: str) -> list[OcSession]:
        """某個 Session 的直接子 Session（遞迴用；沒有就回空）。"""
        data = self._request(f"/session/{urllib.parse.quote(session_id)}/children")
        if data is None:
            return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        return [_parse_session(item) for item in data]

    def walk(self) -> list[OcSession]:
        """清單 ＋ 逐層遞迴補齊子 Session（1.7h：遞迴列舉可行）。

        `GET /session` 已經含子 Session，這裡再補是為了「該列出來但清單漏掉」
        的情況（不同版本行為不同）；去重後依 id 排序。
        """
        found: dict[str, OcSession] = {s.id: s for s in self.list_sessions()}
        frontier = list(found.values())
        seen: set[str] = set()
        while frontier:
            current = frontier.pop()
            if current.id in seen:
                continue
            seen.add(current.id)
            for child in self.children(current.id):
                if child.id not in found:
                    found[child.id] = child
                    frontier.append(child)
                elif child.id not in seen:
                    frontier.append(child)
        return sorted(found.values(), key=lambda s: s.id)

    def archive(self, session_id: str, at_ms: int) -> None:
        """設定 `time.archived`（停止的**唯一**明確管道；1.7e）。

        解除要用 0（`{"archived": null}` 不會清除，1.7e）。
        """
        self._request(
            f"/session/{urllib.parse.quote(session_id)}",
            method="PATCH",
            payload={"time": {"archived": int(at_ms)}},
        )

    # ── CLI ─────────────────────────────────────────────────────────────

    def export(self, session_id: str, dest: Path) -> Path:
        """把某個 Session 匯出成 JSON 檔（stdout 位元組原樣）。

        **一定要明示 id**：`opencode export` 不帶參數會進互動選單（1.7a）。
        """
        if not session_id or not session_id.strip():
            raise ValueError("export 必須明示 session_id（不帶 id 會進互動選單）")
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        # 真的切到 Session 目錄（只設 PWD 不會 chdir；1.7b 要讀到該目錄的訊息庫）
        cwd = self.directory if self.directory and Path(self.directory).is_dir() else None
        proc = subprocess.run(
            [self.opencode_bin, "export", session_id],
            capture_output=True,
            timeout=self.export_timeout,
            check=False,
            cwd=cwd,
            env=_cli_env(self.directory),
        )
        if proc.returncode != 0:
            stderr = proc.stderr.decode("utf-8", errors="replace").strip()
            raise ReadError(
                f"opencode export 失敗 (rc={proc.returncode}) session={session_id}: "
                f"{stderr[:200]}"
            )
        data = proc.stdout
        if not data:
            raise ReadError(f"opencode export 沒有輸出 session={session_id}")
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise ReadError(f"opencode export 的輸出不是合法 JSON: {e}") from None
        out.write_bytes(data)
        return out

    def ping(self) -> bool:
        """API 是否可用（daemon 啟動時先探一下，失敗不要整個容器退出）。"""
        try:
            self.list_sessions()
        except ReadError:
            return False
        return True


def _cli_env(directory: str) -> dict[str, str]:
    """`opencode export` 的環境：固定工作目錄，避免受呼叫端 CWD 影響。"""
    import os

    env = dict(os.environ)
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    if directory:
        env["PWD"] = directory
    return env


def iter_main_sessions(sessions: Iterable[OcSession]) -> list[OcSession]:
    """只留主 Session（plugin 的「主 Session 限定」檢查在 Python 端也要有）。"""
    return [s for s in sessions if s.is_main]
