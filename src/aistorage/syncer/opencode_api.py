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
  - **stdout 必須是「一般檔案」**（impl1／9.5 e2e 實測）：stdout 是 pipe 時，
    輸出超過約 64 KiB 就會以 rc=0 回傳**被截斷**的 JSON。見 `export()` 的說明。
- 停止只認 `time.archived > 0`（明確操作，不是閒置推測）。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
from typing import Any, Iterable
import urllib.error
import urllib.parse
import urllib.request

from aistorage.errors import IncompleteFetch, ReadError

DEFAULT_BASE_URL = "http://127.0.0.1:4096"
DEFAULT_DIRECTORY = "/work"
REQUEST_TIMEOUT_S = 30.0
EXPORT_TIMEOUT_S = 120.0
#: 匯出檔的暫存後綴：驗證通過才換成正式檔名，失敗不會留下半份。
PARTIAL_SUFFIX = ".partial"


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

    def message_ids(self, session_id: str) -> list[str]:
        """`GET /session/{id}/message` 裡依序的訊息 id（驗證匯出完整時用）。

        這是**交叉檢查**用的輸入，不是拿來組匯出檔的（見 `export()` 為什麼不用
        API 重新組：`opencode export` 與 API 回傳的欄位順序不同，重組出來的位元組
        對不上，而原始紀錄必須與 export 位元組相同）。
        """
        data = self._request(f"/session/{urllib.parse.quote(session_id)}/message")
        if not isinstance(data, list):
            raise ReadError(
                f"opencode API /session/{session_id}/message 的回應不是清單"
            )
        out: list[str] = []
        for item in data:
            info = item.get("info") if isinstance(item, dict) else None
            mid = info.get("id") if isinstance(info, dict) else None
            if isinstance(mid, str) and mid:
                out.append(mid)
        return out

    # ── CLI ─────────────────────────────────────────────────────────────

    def export(self, session_id: str, dest: Path) -> Path:
        """把某個 Session 匯出成 JSON 檔（stdout 位元組原樣）。

        **一定要明示 id**：`opencode export` 不帶參數會進互動選單（1.7a）。

        ## 為什麼 stdout 要開成「一般檔案」，不能用 pipe

        9.5 e2e 實測（opencode 1.18.32，完整記錄見
        `docs/spike/evidence/impl1-export-truncation.md`）：`opencode export` 寫完
        stdout 就結束行程，**不會等 pipe 排空**。stdout 是 pipe／fifo 時，子行程結束
        前只來得及送出 64 KiB 的倍數（正好是 Linux pipe 緩衝區），**rc 仍是 0**，
        內容卻是被截斷的 JSON。實測（每個大小 3 次，11 個大小從 21 KB 到 1.6 MB）：

        | 匯出大小 | pipe 取法 | 檔案取法 |
        |---|---|---|
        | 21 KB、62 KB | 6/6 完整 | 6/6 完整 |
        | 103 KB〜1.6 MB（10 個匯出） | **10/10 至少壞一次、29/30 次被截斷**；壞的長度在 64／128 KiB 跳動 | 30/30 完整 |

        也就是說：> 約 64 KiB 就在踩雷，1.6 MB 連續 6/6 全被截斷。stdout 導到一般檔案
        時走的是另一條寫入路徑，同一個匯出一律完整（實測到 1.6 MB）。

        所以這裡把 `dest` 的暫存檔直接當子行程的 stdout（`stdout=<檔案物件>`）。
        **不用 `sh -c`**：那樣 session id 會被 shell 二次解讀，而 1.7a 對 id 的格式
        驗得很鬆。拿到的就是 export 自己的位元組，不是「重新組出來的」。

        這也順帶回答了「為什麼不用 HTTP API 逐則取訊息再組回」：`opencode export`
        與 `GET /session/{id}`／`/message` 回傳的**欄位順序不同**（export 依 DB 欄位
        順序，API 依自己的順序），重組出來的內容雖然等價，位元組卻對不上。原始紀錄
        要原封不動、checkout／轉接器的開頭要位元組相同（ADR 0010），所以只能走檔案。

        ## 完整性驗證

        **不完整就整個拒收**（丟 `IncompleteFetch`），不要上傳半份——半份原始紀錄一旦
        進了 Agora 就再也分不出來。驗三件事：
        1. 輸出是合法 JSON；
        2. 形狀是 `{info, messages}`，而且 `info.id` 就是要求的 id；
        3. 匯出最後一則訊息在 `GET /session/{id}/message` 上找得到。

        第 3 點**不要求訊息數相等**：Session 可能正在跑、匯出之後又長出新訊息，
        也可能剛被 revert 掉尾巴（這兩種都會讓 API 與匯出的數量不一致，但匯出都是
        完整的）。只問「匯出的最後一則，API 上還在不在」——在，就代表匯出至少涵蓋了
        到那一則為止的完整前綴。
        """
        if not session_id or not session_id.strip():
            raise ValueError("export 必須明示 session_id（不帶 id 會進互動選單）")
        out = Path(dest)
        out.parent.mkdir(parents=True, exist_ok=True)
        partial = out.with_name(out.name + PARTIAL_SUFFIX)
        # 真的切到 Session 目錄（只設 PWD 不會 chdir；1.7b 要讀到該目錄的訊息庫）
        cwd = self.directory if self.directory and Path(self.directory).is_dir() else None
        try:
            with open(partial, "wb") as sink:
                proc = subprocess.run(
                    [self.opencode_bin, "export", session_id],
                    stdout=sink,   # 一般檔案：export 的 stdout 會確實寫完（見上）
                    stderr=subprocess.PIPE,
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
            data = partial.read_bytes()
            if not data:
                raise ReadError(f"opencode export 沒有輸出 session={session_id}")
            self._verify_export(session_id, data)
        except BaseException:
            # 成功以外一律不留暫存檔：dest 上也不能有上一次留下的舊檔被誤用
            partial.unlink(missing_ok=True)
            raise
        # 驗證通過才換成正式檔名（同一個檔案系統上是 atomic rename）
        os.replace(partial, out)
        return out

    def _verify_export(self, session_id: str, data: bytes) -> dict[str, Any]:
        """驗證匯出是完整的 Session；不完整丟 `IncompleteFetch`（別混在 ReadError）。"""
        try:
            document = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as e:
            raise IncompleteFetch(
                f"取得不完整：opencode export 的輸出不是合法 JSON"
                f"（session={session_id}，{len(data)} bytes）：{e}"
            ) from None
        if not isinstance(document, dict) or not isinstance(document.get("messages"), list):
            raise IncompleteFetch(
                f"取得不完整：opencode export 的形狀不符"
                f"（session={session_id}，缺 messages 或不是清單）"
            )
        info = document.get("info")
        got = info.get("id") if isinstance(info, dict) else None
        if got != session_id:
            raise IncompleteFetch(
                f"取得不完整：匯出的是別的 Session"
                f"（session={session_id}，匯出裡的 id 是 {got!r}）"
            )
        messages = document["messages"]
        tail: str | None = None
        for message in messages:
            if not isinstance(message, dict):
                continue
            minfo = message.get("info")
            mid = minfo.get("id") if isinstance(minfo, dict) else None
            if isinstance(mid, str) and mid:
                tail = mid
        if tail is None:
            return document
        known = set(self.message_ids(session_id))
        if tail not in known:
            raise IncompleteFetch(
                f"取得不完整：匯出的最後一則訊息（{tail}）在 opencode API 上不存在"
                f"（session={session_id}；匯出 {len(messages)} 則／API {len(known)} 則）"
            )
        return document

    def ping(self) -> bool:
        """API 是否可用（daemon 啟動時先探一下，失敗不要整個容器退出）。"""
        try:
            self.list_sessions()
        except ReadError:
            return False
        return True


def _cli_env(directory: str) -> dict[str, str]:
    """`opencode export` 的環境：固定工作目錄，避免受呼叫端 CWD 影響。"""
    env = dict(os.environ)
    env["OPENCODE_DISABLE_PROJECT_CONFIG"] = "1"
    if directory:
        env["PWD"] = directory
    return env


def iter_main_sessions(sessions: Iterable[OcSession]) -> list[OcSession]:
    """只留主 Session（plugin 的「主 Session 限定」檢查在 Python 端也要有）。"""
    return [s for s in sessions if s.is_main]
