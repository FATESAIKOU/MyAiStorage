#!/usr/bin/env python3
"""1.4f3：以內容判定、遞迴清掃 repo 資料夾（H1／H2／M2）。

可信集合由內容推導，不從列舉得來：
- GITMANIFEST / .bak：內容 sha256 必須在可信 manifest 雜湊集合內
  （promoted、promoted.prev、pending、以及 --extra-manifest-shas 由內容重放得到的候選）。
  同名同內容的重複只留一個，其餘隔離。
- GITBUNDLE：必須被某個可信 manifest 的內容引用、且檔名的 size/sha256 與內容相符；
  同名同內容的重複只留一個。未被引用或雜湊不符 → 隔離。
- annex 物件（SHA256E-…）：檔名 sha256 與 size 必須與內容相符（結構有效）→ 保留；
  不符 → 隔離。
- 其餘檔案 → 隔離。子資料夾遞迴走訪，逐檔判定。

任何移動失敗、或（例行清掃時）沒有可信集合 → exit 非 0（提交流程必須中止本輪）。

用法：
  f3_clean.py --cred <conf> --folder <id> --pin <promoted.json>
      [--pending-pin <pending.json>] [--extra-manifest-shas a,b]
      --quarantine <id> [--apply]
"""

import argparse
import hashlib
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_probe import API, call, load_token, reason_of  # noqa: E402

BUNDLE_RE = re.compile(r"^GITBUNDLE-s(\d+)--([0-9a-f-]+)-([0-9a-f]{64})$")
ANNEX_RE = re.compile(r"^SHA256E-s(\d+)--([0-9a-f]{64})(\..*)?$")
MANIFEST_RE = re.compile(r"^GITMANIFEST--[0-9a-f-]+(\.bak)?$")


def list_children(token, folder_id):
    files = []
    page = None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "nextPageToken,files(id,name,mimeType,size)",
            "pageSize": 200,
        }
        if page:
            params["pageToken"] = page
        r = call("GET", f"{API}/files", token, params=params)
        if not r.ok:
            reason, msg = reason_of(r)
            print(f"list.http={r.status_code} reason={reason} message={msg}")
            sys.exit(2)
        body = r.json()
        files += body.get("files", [])
        page = body.get("nextPageToken")
        if not page:
            break
    return files


def download(token, file_id):
    r = call("GET", f"{API}/files/{file_id}", token, params={"alt": "media"})
    return r.content if r.ok else None


def load_trust(pins, extra_manifest_shas):
    manifest_shas, bundle_names = set(), set()
    for pin in pins:
        if not pin:
            continue
        for key in ("manifest_sha256", "prev_manifest_sha256"):
            if pin.get(key):
                manifest_shas.add(pin[key])
        for b in pin.get("manifest_bundles", []):
            bundle_names.add(b)
    manifest_shas.update(extra_manifest_shas)
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
    p.add_argument("--extra-manifest-shas", default="",
                   help="逗號分隔；內容重放判定為可信的 manifest 內容雜湊")
    p.add_argument("--quarantine", required=True)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--allow-empty-trust", action="store_true",
                   help="僅限首次初始化：沒有釘選值時允許清空（例行清掃誤用會隔離全部）")
    args = p.parse_args()

    token, _ = load_token(args.cred)
    pins = [json.load(open(args.pin))] if args.pin and os.path.exists(args.pin) else []
    if args.pending_pin and os.path.exists(args.pending_pin):
        pins.append(json.load(open(args.pending_pin)))
    extra = {s for s in args.extra_manifest_shas.split(",") if s}
    trusted_manifest, trusted_bundles = load_trust(pins, extra)
    if not trusted_manifest and not args.allow_empty_trust:
        print("CLEAN-REFUSED: 沒有可信集合（例行清掃不得清空；首次初始化請用 --allow-empty-trust）")
        return 2

    kept, moved, failures = [], [], []
    seen_manifest_key = {}
    seen_bundle_key = {}
    seen_annex_key = {}

    queue = [(args.folder, "")]
    while queue:
        folder_id, rel = queue.pop(0)
        for f in list_children(token, folder_id):
            name, fid = f["name"], f["id"]
            path = f"{rel}/{name}"
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                # 目前 remote layout 是平的（實測）：任何子資料夾都不在可信結構內 →
                # 整個子樹視為不可信，搬走（不遞迴進去，避免漏掉裡面的注入物，M2）。
                # 若未來 layout 改成巢狀 annex 物件，信任規則要連 layout 一起定義。
                moved.append((path + "/", fid, "subfolder-非可信結構（含子樹）"))
                if args.apply and not move(token, fid, folder_id, args.quarantine, path, failures):
                    print(f"  [MOVE-FAILED] {path} id={fid}")
                continue
            data = download(token, fid)
            content_sha = hashlib.sha256(data).hexdigest() if data is not None else None
            size = len(data) if data is not None else None
            why, keep = "", False
            if MANIFEST_RE.match(name):
                if content_sha in trusted_manifest:
                    key = (name, content_sha)
                    first = seen_manifest_key.get(key)
                    if first is None:
                        seen_manifest_key[key] = fid
                        keep, why = True, "manifest-內容相符"
                    else:
                        why = f"manifest-同內容重複（已有 id={first}）"
                else:
                    why = "manifest-內容不符"
            elif BUNDLE_RE.match(name):
                m = BUNDLE_RE.match(name)
                name_ok = data is not None and content_sha == m.group(3) and size == int(m.group(1))
                if name in trusted_bundles and name_ok:
                    key = (name, content_sha)
                    first = seen_bundle_key.get(key)
                    if first is None:
                        seen_bundle_key[key] = fid
                        keep, why = True, "bundle-被引用且雜湊相符"
                    else:
                        why = f"bundle-同內容重複（已有 id={first}）"
                else:
                    why = "bundle-未被引用或雜湊不符"
            elif ANNEX_RE.match(name):
                m = ANNEX_RE.match(name)
                name_ok = data is not None and content_sha == m.group(2) and size == int(m.group(1))
                if name_ok:
                    key = (name, content_sha)
                    first = seen_annex_key.get(key)
                    if first is None:
                        seen_annex_key[key] = fid
                        keep, why = True, "annex-object-結構有效"
                    else:
                        why = f"annex-object-同內容重複（已有 id={first}）"
                else:
                    why = "annex-object-名稱與內容不符"
            else:
                why = "unknown-不在可信集合"

            if keep:
                kept.append((path, fid, why))
            else:
                moved.append((path, fid, why))
                if args.apply and not move(token, fid, folder_id, args.quarantine, path, failures):
                    print(f"  [MOVE-FAILED] {path} id={fid}")

    print(f"op=clean folder={args.folder} trusted_manifest={len(trusted_manifest)} trusted_bundles={len(trusted_bundles)}")
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
