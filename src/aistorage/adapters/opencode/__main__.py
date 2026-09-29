"""`agora-opencode`：把 Agora 的起點包載入成 opencode 的原生 session。

    agora-opencode load <起點包> [-C <專案目錄>]

只印新 session id（`--json` 會多印一點）。**開不開 agent、在哪開，由呼叫者
決定**（`opencode --session <新 id>`）。

這個套件是 opencode 專屬的；`aistorage.agora_cli` 不會 import 它。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from aistorage.adapters.opencode.loader import (
    AdapterError,
    build_export,
    load,
)
from aistorage.agora_cli.package import ContextPackageError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agora-opencode",
        description="把 Agora 的起點包載入成 opencode 的原生 session")
    sub = parser.add_subparsers(dest="command", required=True)

    load_p = sub.add_parser("load", help="載入起點包，印出新 session id")
    load_p.add_argument("package", help="起點包目錄（含 package.json）")
    load_p.add_argument("-C", "--workdir", default=".",
                        help="要開在哪個專案目錄（import 會把 session 掛在這裡）")
    load_p.add_argument("--session-id", default=None,
                        help="（只能等於起點包預留的 id）覆寫新 session id；"
                             "預設直接用起點包裡的。換 id 會讓 Agora 裡那筆預留"
                             "沒有人接手，所以不符就拒絕")
    load_p.add_argument("--json", action="store_true", help="輸出 JSON 而不是只有 id")
    load_p.add_argument("--print-export", action="store_true",
                        help="只印組好的匯出 JSON，不呼叫 opencode（除錯用）")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.print_export:
            payload, session_id, segments, source = build_export(
                Path(args.package), session_id=args.session_id)
            print(json.dumps(
                {"session_id": session_id, "source": source,
                 "segments": segments,
                 "messages": len(payload["messages"]), "export": payload},
                ensure_ascii=False, sort_keys=True))
            return 0
        result = load(Path(args.package), workdir=Path(args.workdir),
                      session_id=args.session_id)
    except (AdapterError, ContextPackageError) as e:
        print(f"[agora-opencode] {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({
            "session_id": result.session_id,
            "messages": result.messages,
            "segments": result.segments,
            "import_output": result.import_output,
        }, ensure_ascii=False, sort_keys=True))
    else:
        # 呼叫者只要 id：`opencode --session <id>` 就帶著前面的內容了。
        print(result.session_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
