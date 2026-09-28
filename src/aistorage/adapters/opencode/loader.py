"""轉接器：把起點包載入成 opencode 的原生 session（`agora-opencode load`）。

**這個模組是 opencode 專屬的**。`aistorage.agora_cli` 不 import 這裡的任何東西
——Agora 只管產出起點包，載入成哪個 agent 的原生 session 是各轉接器的事
（`agora-<coding agent 名稱>`）。

做法固定（`docs/spike/session-import.md`，opencode 1.18.32 實測）：

> 匯出 JSON 截斷到接續點 → **session／message／part id 全部重編** →
> 在目標專案目錄執行 `opencode import`。

三個不能省的規則：
1. **id 一定要重編**。`import` 對已存在的 message／part id 是 `onConflictDoNothing`
   **靜默丟棄**：沿用原 id 匯入兩次，第二次得到的是空 session（Q1-1）。
2. **在目標專案目錄匯入**。`import` 強制把 directory／project/path 改寫為當下的
   context，目錄寫在匯出檔裡也沒用（Q1-2）。
3. **n→1 要手工鏈 parent**。`import` 不驗 parent（Q1-3），所以時序與鏈接由這裡
   保證。

`load` 印出新 session id 就結束。**開不開 agent、在哪開，是呼叫者的事**。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Sequence

from aistorage.agora_cli.package import ContextPackageError, read_package

#: 匯入後預設的 session 標題前綴（`package.json` 的 `task` 會接在後面）。
TITLE_PREFIX = "Agora 起點"

__all__ = [
    "AdapterError",
    "LoadResult",
    "build_export",
    "chain_parents",
    "load",
    "reidentify",
    "run_import",
    "truncate_to",
]


class AdapterError(RuntimeError):
    """轉接器失敗。訊息裡只有 id 與代碼，不含 Session 內文。"""


@dataclass(frozen=True)
class LoadResult:
    """一次 `load` 的結果。"""

    session_id: str
    source: str
    messages: int
    segments: int
    import_output: str = ""


def _messages(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        raise AdapterError("匯出檔的最外層必須是 JSON 物件")
    msgs = payload.get("messages")
    if not isinstance(msgs, list):
        raise AdapterError("匯出檔缺少 messages 清單")
    for position, m in enumerate(msgs):
        if not isinstance(m, dict) or not isinstance(m.get("info"), dict):
            raise AdapterError(f"第 {position} 則訊息形狀不對（缺 info 物件）")
    return msgs


def truncate_to(payload: dict, message_id: str | None) -> list[dict]:
    """截到接續點為止（**含**該則）；`message_id` 為 None 就整份不截。

    接續點是「最後一則已完成的訊息」，所以**那一則要含在內**——新 session 從
    它之後開始寫，模型才看得到交接的那一段（`reading.messages_before` 的同一個
    語意：`<= target_index`）。
    """
    msgs = _messages(payload)
    if message_id is None:
        return msgs
    for position, m in enumerate(msgs):
        if str(m["info"].get("id")) == message_id:
            return msgs[: position + 1]
    known = len(msgs)
    raise AdapterError(
        f"這個快照裡沒有訊息 {message_id}（實際有 {known} 則）；"
        "起點包的 snapshot_sha256 與訊息 id 對不上，不能拿錯位置去接續"
    )


def reidentify(payload: dict, messages: Sequence[dict], *, session_id: str,
               tag: str) -> dict:
    """把截斷後的訊息**全部 id 重編**成新 session 的（回傳新的匯出字典）。

    前綴保留（`ses_`／`msg_`／`prt_`，spike Q1-4：id 格式寬鬆但這是慣例），
    後綴換成這一次專屬的。`parentID` 與 `part.messageID` 依對應表改寫，
    **其餘欄位一個位元組都不動**——動了開頭就不會與原 session 位元組相同。
    """
    out = dict(payload)
    info = dict(payload.get("info") or {})
    info["id"] = session_id
    out["info"] = info

    message_map: dict[str, str] = {}
    new_messages: list[dict] = []
    for i, m in enumerate(messages):
        m = dict(m)
        m_info = dict(m["info"])
        old = str(m_info.get("id"))
        new = f"msg_{tag}{i:020d}"[:30]
        message_map[old] = new
        m_info["id"] = new
        m_info["sessionID"] = session_id
        parent = m_info.get("parentID")
        if isinstance(parent, str) and parent in message_map:
            m_info["parentID"] = message_map[parent]
        elif isinstance(parent, str) and parent:
            # parent 指向這次截斷範圍之外的訊息（截斷把它切掉了）：明確清掉，
            # 不要留著指向不存在的訊息。
            m_info["parentID"] = None
        m["info"] = m_info
        parts: list[dict] = []
        for j, p in enumerate(m.get("parts") or []):
            p = dict(p)
            p["id"] = f"prt_{tag}{i:010d}{j:06d}"[:30]
            p["sessionID"] = session_id
            p["messageID"] = new
            parts.append(p)
        m["parts"] = parts
        new_messages.append(m)
    out["messages"] = new_messages
    return out


def chain_parents(segments: Sequence[Sequence[dict]]) -> list[dict]:
    """n→1：把後一段的**首則** parent 手工鏈到前一段的**末則**（spike Q4）。

    沒有這一步，匯入出來的 session 會把兩段當成兩條平行的線；`import` 不會
    報錯（它不驗 parent），所以錯了要靠這裡擋。
    """
    if len(segments) < 2:
        return list(segments[0]) if segments else []
    merged: list[dict] = []
    for index, segment in enumerate(segments):
        if not segment:
            raise AdapterError(f"第 {index + 1} 段沒有訊息，無法串接")
        if index == 0:
            merged.extend(dict(m) for m in segment)
            continue
        previous_last = merged[-1]["info"]["id"]
        first = dict(segment[0])
        first_info = dict(first["info"])
        first_info["parentID"] = previous_last
        first["info"] = first_info
        merged.append(first)
        merged.extend(dict(m) for m in segment[1:])
    return merged


def build_export(package_dir: Path, *, session_id: str | None = None,
                 tag: str | None = None) -> tuple[dict, str, int, str]:
    """把起點包組成一份可以 `opencode import` 的匯出 JSON。

    回傳 `(匯出字典, 新 session 的來源端 id, 段數, 來源應用)`。**這一步不碰
    opencode**——所以可以被單元測試直接驗（「重編 id」與「n→1 串接」）。
    """
    data, raws = read_package(Path(package_dir))
    new_session = data.get("new_session") or {}
    session_ref = session_id or str(new_session.get("session_id") or "")
    if not session_ref:
        raise ContextPackageError(
            "起點包沒有 new_session.session_id："
            "轉接器必須沿用它（收件匣裡的認領記的是這個 id）"
        )
    # Agora 的 Session id 是 `<source>:<native>`；opencode 的匯出檔用的是
    # 只有 `<native>` 的形式（`info.id`）。收件匣與同步器那邊認的是完整形式，
    # 所以完整形式留在 `package.json`，匯出時去掉前綴。
    source = str(new_session.get("source") or "opencode")
    target = session_ref.split(":", 1)[1] if ":" in session_ref else session_ref
    use_tag = tag or _tag_for(target)

    segments: list[list[dict]] = []
    for position, (segment, raw) in enumerate(zip(data["segments"], raws)):
        if segment.get("source") != new_session.get("source"):
            # 跨來源的片段沒有辦法「原封不動」重建開頭（ADR 0010）。期 1 只做
            # 同應用，明確拒絕而不是給一個開頭已被改寫的 session。
            raise AdapterError(
                f"第 {position + 1} 段是 {segment.get('source')!r}，"
                f"但新 session 是 {new_session.get('source')!r}："
                "跨來源應用要改走共通閱讀版轉換（開頭會被改寫、KV cache 會斷），"
                "期 1 只支援同來源。請把兩邊都換成同一個來源應用再接續。"
            )
        payload = _loads(raw, segment)
        messages = truncate_to(payload, segment.get("message_id"))
        # 每段各自重編（1→n 時同一份內容要能被匯入 n 次，所以 id 不能重複）。
        segments.append(list(reidentify(payload, messages,
                                        session_id=target,
                                        tag=f"{use_tag}{position}X")["messages"]))

    merged = chain_parents(segments)
    if not merged:
        raise AdapterError("起點包沒有任何訊息，沒有東西可以載入")

    # 最終形狀：info 取第一段的（同一個來源、同一次匯出），messages 是串接結果。
    first = _loads(raws[0], data["segments"][0])
    out = dict(first)
    info = dict(first.get("info") or {})
    info["id"] = target
    out["info"] = info
    out["messages"] = merged
    return out, target, len(segments), source


def _loads(raw: bytes, segment: Any) -> dict:
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise ContextPackageError(
            f"起點包裡的原始紀錄不是合法 JSON（{segment.get('raw_file')}）：{e}"
        ) from None
    if not isinstance(payload, dict):
        raise AdapterError("原始紀錄的最外層必須是 JSON 物件")
    return payload


def _tag_for(session_id: str) -> str:
    """從新 session id 取出重編 id 用的 tag（要短且唯一到 30 字元以內）。"""
    tail = session_id.rsplit("_", 1)[-1]
    return (tail or session_id)[-10:]


def run_import(payload: dict, *, workdir: Path,
               runner: Callable[[Sequence[str], Path], Any] | None = None
               ) -> str:
    """在 `workdir` 執行 `opencode import`（spike Q1-2：必須在目標專案目錄）。

    `runner` 供測試注入；預設真的呼叫 `opencode`。
    """
    with tempfile.TemporaryDirectory(prefix="agora-opencode-") as td:
        export_path = Path(td) / "import.json"
        export_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        argv = ["opencode", "import", str(export_path)]
        if runner is not None:
            result = runner(argv, workdir)
            return str(result)
        proc = subprocess.run(  # noqa: S603 - argv 固定形狀，沒有使用者輸入
            argv, cwd=str(workdir), capture_output=True, text=True, check=False,
        )
        if proc.returncode != 0:
            raise AdapterError(
                f"opencode import 失敗 (rc={proc.returncode}): "
                f"{proc.stderr.strip()[-500:]}"
            )
        return (proc.stdout or "").strip()


def load(package_dir: Path, *, workdir: Path,
         runner: Callable[[Sequence[str], Path], Any] | None = None,
         session_id: str | None = None) -> LoadResult:
    """`agora-opencode load <起點包>`：載入成原生 session，回傳新 session id。"""
    payload, target, segments, source = build_export(
        Path(package_dir), session_id=session_id)
    output = run_import(payload, workdir=Path(workdir), runner=runner)
    return LoadResult(
        session_id=target,
        source=source,
        messages=len(payload["messages"]),
        segments=segments,
        import_output=output,
    )
