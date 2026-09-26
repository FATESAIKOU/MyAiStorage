#!/usr/bin/env python3
"""1.4f4（H4 前半）：只用 Drive metadata 的遞迴清掃（不下載 bundle／annex 物件）。
判定與 f3_clean.py 相同，但 valid 來自 metadata 的 sha256Checksum＋size：
- GITMANIFEST / .bak：metadata sha256Checksum 在釘選值的可信集合內
- GITBUNDLE：被可信 manifest 引用、且檔名內嵌 size/sha256 == metadata
- annex 物件：檔名內嵌 size/sha256 == metadata
只有可信 manifest（小檔）需要下載解析引用清單，因此 snapshot 的 manifests[].bundles 必須
先由 f3_meta_snapshot.py（或 --snapshot 參數）提供。沒有可信 manifest 的引用清單時，
bundle 一律隔離（fail-closed）。

用法：
  f3_meta_clean.py --cred <conf> --folder <id> --pin <promoted.json>
      [--pending-pin <pending.json>] --snapshot <meta-snapshot.json>
      --quarantine <id> [--apply]
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_probe import API, call, load_token, reason_of  # noqa: E402
from f3_meta_snapshot import list_children_meta  # noqa: E402

BUNDLE_RE = re.compile(r"^GITBUNDLE-s(\d+)--([0-9a-f-]+)-([0-9a-f]{64})$")
ANNEX_RE = re.compile(r"^SHA256E-s(\d+)--([0-9a-f]{64})(\..*)?$")
MANIFEST_RE = re.compile(r"^GITMANIFEST--[0-9a-f-]+(\.bak)?$")


def load_trust(pin_paths):
    manifest_shas, bundle_names = set(), set()
    for p in pin_paths:
        if p and os.path.exists(p):
            doc = json.load(open(p))
            for key in ("manifest_sha256", "prev_manifest_sha256"):
                if doc.get(key):
                    manifest_shas.add(doc[key])
            for b in doc.get("manifest_bundles", []):
                bundle_names.add(b)
    return manifest_shas, bundle_names


def move(token, fid, parent, quarantine, tag, failures):
    r = call("PATCH", f"{API}/files/{fid}", token,
             params={"addParents": quarantine, "removeParents": parent,
                     "fields": "id,name,parents"}, json={})
    if not r.ok:
        reason, _ = reason_of(r)
        failures.append((tag, fid, f"http={r.status_code} reason={reason}"))
        return False
    return True


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cred", required=True)
    p.add_argument("--folder", required=True)
    p.add_argument("--pin", required=True)
    p.add_argument("--pending-pin", default=None)
    p.add_argument("--snapshot", required=True, help="f3_meta_snapshot.py 的輸出（含引用清單）")
    p.add_argument("--quarantine", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--allow-empty-trust", action="store_true")
    args = p.parse_args()

    token, _ = load_token(args.cred)
    manifest_shas, bundle_names = load_trust([args.pin, args.pending_pin])
    if not manifest_shas and not args.allow_empty_trust:
        print("CLEAN-REFUSED: 沒有可信集合（例行清掃不得清空；首次初始化請用 --allow-empty-trust）")
        return 2

    # 來自 snapshot：被信任 manifest 的引用清單（已下載解析）
    snap = json.load(open(args.snapshot))
    snap_bundles = set()
    for m in snap.get("manifests", []):
        if m.get("trusted"):
            snap_bundles.update(m.get("bundles", []))
    bundle_names |= snap_bundles

    kept, moved, failures = [], [], []
    seen_manifest_key, seen_bundle_key, seen_annex_key = {}, {}, {}
    queue = [(args.folder, "")]
    while queue:
        folder_id, rel = queue.pop(0)
        for f in list_children_meta(token, folder_id):
            name, fid = f["name"], f["id"]
            path = f"{rel}/{name}"
            size = int(f["size"]) if f.get("size") else None
            sha = f.get("sha256Checksum")
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                moved.append((path + "/", fid, "subfolder-非可信結構（含子樹）"))
                if args.apply and not move(token, fid, folder_id, args.quarantine, path, failures):
                    print(f"  [MOVE-FAILED] {path} id={fid}")
                continue
            why, keep = "", False
            if MANIFEST_RE.match(name):
                if sha in manifest_shas:
                    key = (name, sha)
                    first = seen_manifest_key.get(key)
                    if first is None:
                        seen_manifest_key[key] = fid
                        keep, why = True, "manifest-內容相符（metadata）"
                    else:
                        why = f"manifest-同內容重複（已有 id={first}）"
                else:
                    why = "manifest-內容不符（metadata）"
            elif BUNDLE_RE.match(name):
                m = BUNDLE_RE.match(name)
                name_ok = sha == m.group(3) and size == int(m.group(1))
                if name in bundle_names and name_ok:
                    key = (name, sha)
                    first = seen_bundle_key.get(key)
                    if first is None:
                        seen_bundle_key[key] = fid
                        keep, why = True, "bundle-被引用且雜湊相符（metadata）"
                    else:
                        why = f"bundle-同內容重複（已有 id={first}）"
                else:
                    why = "bundle-未被引用或雜湊不符（metadata）"
            elif ANNEX_RE.match(name):
                m = ANNEX_RE.match(name)
                name_ok = sha == m.group(2) and size == int(m.group(1))
                if name_ok:
                    key = (name, sha)
                    first = seen_annex_key.get(key)
                    if first is None:
                        seen_annex_key[key] = fid
                        keep, why = True, "annex-object-結構有效（metadata）"
                    else:
                        why = f"annex-object-同內容重複（已有 id={first}）"
                else:
                    why = "annex-object-名稱與內容不符（metadata）"
            else:
                why = "unknown-不在可信集合"
            if keep:
                kept.append((path, fid, why))
            else:
                moved.append((path, fid, why))
                if args.apply and not move(token, fid, folder_id, args.quarantine, path, failures):
                    print(f"  [MOVE-FAILED] {path} id={fid}")

    print(f"op=meta-clean folder={args.folder} trusted_manifest={len(manifest_shas)} trusted_bundles={len(bundle_names)}")
    print(f"kept_count={len(kept)} moved_count={len(moved)} move_failures={len(failures)}")
    for path, fid, why in kept:
        print(f"  [KEEP] {path} id={fid} ({why})")
    for path, fid, why in moved:
        action = "moved" if args.apply else "would_move"
        print(f"  [{action}] {path} id={fid} ({why})")
    for tag, fid, why in failures:
        print(f"  [MOVE-FAILED] {tag} id={fid} {why}")
    if failures:
        print("CLEAN-FAILED: 有移動失敗，提交流程必須中止本輪")
        return 2
    print("CLEAN-OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
