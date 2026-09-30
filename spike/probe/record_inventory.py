#!/usr/bin/env python3
"""1.4f2 對策 A：記錄清冊（file id 清單＋釘選值）。

用法：
  spike/probe/record_inventory.py --cred <conf> --folder <repo資料夾id> \
      --prefix <rcloneprefix> --pinned-head <commit hash> --out <清冊.json>

清冊內容（模擬 GitHub 端的「清冊＋tip 釘選」）：
  {
    "repo_prefix": "agora-1.4f2/",
    "recorded_at": "<UTC ISO8601>",
    "pinned_head": "<commit hash>",
    "files": [ {"id": ..., "name": ..., "md5Checksum": ..., "size": ...}, ... ]
  }
輸出只有 file id 與檔名等非秘密資訊。token 只進記憶體與 HTTP header。
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scan_and_quarantine import list_children  # noqa: E402
from drive_probe import load_token  # noqa: E402


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cred", required=True)
    p.add_argument("--folder", required=True)
    p.add_argument("--prefix", required=True)
    p.add_argument("--pinned-head", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()

    token, _ = load_token(args.cred)
    files = list_children(token, args.folder)
    doc = {
        "repo_prefix": args.prefix,
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "pinned_head": args.pinned_head,
        "files": [
            {k: f.get(k) for k in ("id", "name", "md5Checksum", "size", "createdTime")}
            for f in files
        ],
    }
    with open(args.out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(f"inventory_written={args.out} files={len(files)} pinned_head={args.pinned_head}")
    for f in files:
        print(f"  id={f['id']} name={f['name']}")


if __name__ == "__main__":
    main()
