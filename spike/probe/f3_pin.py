#!/usr/bin/env python3
"""1.4f3：釘選檔的兩階段狀態管理（H1）。

子命令：
  record-pending --pin <file> --main-ref <hash> --annex-ref <hash> [--uuid <repo uuid>]
      push 前寫入待定釘選值（只記我們即將推送的 ref hash；內容欄位留空，
      push 後由 f3_validate.py 以內容重放解析出實際內容並轉正）。
  promote --pin <file> --main-ref <h> --annex-ref <h> --manifest-sha <sha>
          --manifest-name <name> --bundles-json '[...]' [--annex-json '{}']
      以驗證過的內容把待定轉正式（含全部內容欄位）。
  show --pin <file>
輸出只有 hash、sha256、名稱；不含秘密。
"""

import argparse
import datetime
import json
import os
import sys


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(path, doc):
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


def load(path):
    with open(path) as fh:
        return json.load(fh)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    rp = sub.add_parser("record-pending")
    rp.add_argument("--pin", required=True)
    rp.add_argument("--main-ref", required=True)
    rp.add_argument("--annex-ref", required=True)
    rp.add_argument("--uuid", default=None)

    pr = sub.add_parser("promote")
    pr.add_argument("--pin", required=True)
    pr.add_argument("--main-ref", required=True)
    pr.add_argument("--annex-ref", required=True)
    pr.add_argument("--manifest-sha", required=True)
    pr.add_argument("--manifest-name", required=True)
    pr.add_argument("--bundles-json", required=True)
    pr.add_argument("--annex-json", default="{}")
    pr.add_argument("--prev-manifest-sha", default=None,
                    help="上一個 manifest 內容雜湊（.bak 的可信規則）")

    sh = sub.add_parser("show")
    sh.add_argument("--pin", required=True)

    args = p.parse_args()

    if args.cmd == "record-pending":
        doc = {
            "state": "pending",
            "refs": {
                "refs/heads/main": args.main_ref,
                "refs/heads/git-annex": args.annex_ref,
            },
            "uuid": args.uuid,
            "recorded_at": now(),
        }
        save(args.pin, doc)
        print(f"pin_written={args.pin} state=pending main={args.main_ref} git-annex={args.annex_ref}")
    elif args.cmd == "promote":
        doc = {
            "state": "promoted",
            "refs": {
                "refs/heads/main": args.main_ref,
                "refs/heads/git-annex": args.annex_ref,
            },
            "manifest_sha256": args.manifest_sha,
            "manifest_name": args.manifest_name,
            "manifest_bundles": json.loads(args.bundles_json),
            "annex_objects": json.loads(args.annex_json),
            "promoted_at": now(),
        }
        if args.prev_manifest_sha:
            doc["prev_manifest_sha256"] = args.prev_manifest_sha
        save(args.pin, doc)
        print(f"pin_promoted={args.pin} main={args.main_ref} git-annex={args.annex_ref} "
              f"manifest_sha={args.manifest_sha[:16]}… bundles={len(doc['manifest_bundles'])} "
              f"annex_objects={len(doc['annex_objects'])}")
    elif args.cmd == "show":
        doc = load(args.pin)
        print(json.dumps(doc, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
