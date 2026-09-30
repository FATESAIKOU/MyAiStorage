#!/usr/bin/env python3
"""1.4f3：列舉 repo 資料夾並以「內容」判定可信候選（H1 的基礎）。

輸出 JSON（不印秘密）：
{
  "folder_id": "...",
  "manifests": [
    {"id","name","sha256","parsed": bool, "bundles": [...],
     "bundles_valid": bool, "invalid_reasons": [...]}      # 每個 GITMANIFEST*
  ],
  "bundles": {"<name>": {"id","sha256","valid": bool}},
  "annex_objects": {"<name>": {"id","sha256","valid": bool}},
  "others": [{"id","name","mimeType"}]
}

判定規則：
- manifest：內容逐行必須是「同資料夾內存在、且內容 sha256/size 與檔名相符」的 GITBUNDLE。
- bundle：檔名 GITBUNDLE-s<N>--<uuid>-<sha256>；sha256 與 size 必須與內容相符。
- annex 物件：檔名 SHA256E-s<N>--<sha256>.<ext>；sha256 與 size 必須與內容相符。
- 其餘檔案與資料夾列在 others。

用法：f3_snapshot.py --cred <conf> --folder <id> --out <json>
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
            "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum)",
            "pageSize": 100,
        }
        if page:
            params["pageToken"] = page
        r = call("GET", f"{API}/files", token, params=params)
        if not r.ok:
            reason, msg = reason_of(r)
            print(f"list.http={r.status_code} reason={reason} message={msg}", file=sys.stderr)
            sys.exit(2)
        body = r.json()
        files += body.get("files", [])
        page = body.get("nextPageToken")
        if not page:
            break
    return files


def download(token, file_id):
    r = call("GET", f"{API}/files/{file_id}", token, params={"alt": "media"})
    if not r.ok:
        return None
    return r.content


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cred", required=True)
    p.add_argument("--folder", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--content-dir", default=None,
                   help="若指定，bundle 內容另存到此目錄（供 f3_replay_check 使用）")
    args = p.parse_args()

    token, _ = load_token(args.cred)
    children = list_children(token, args.folder)
    by_name = {}
    others = []
    for f in children:
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            others.append({"id": f["id"], "name": f["name"], "mimeType": f["mimeType"]})
        elif MANIFEST_RE.match(f["name"]) or BUNDLE_RE.match(f["name"]) or ANNEX_RE.match(f["name"]):
            by_name.setdefault(f["name"], []).append(f)
        else:
            others.append({"id": f["id"], "name": f["name"], "mimeType": f["mimeType"]})

    if args.content_dir:
        os.makedirs(args.content_dir, exist_ok=True)

    bundles = {}
    for name, entries in by_name.items():
        m = BUNDLE_RE.match(name)
        if not m:
            continue
        for e in entries:
            data = download(token, e["id"])
            ok = data is not None and hashlib.sha256(data).hexdigest() == m.group(3) and len(data) == int(m.group(1))
            content_path = None
            if data is not None and args.content_dir:
                content_path = os.path.join(args.content_dir, f"bundle-{e['id']}.bin")
                with open(content_path, "wb") as fh:
                    fh.write(data)
            bundles.setdefault(name, []).append({"id": e["id"], "sha256": hashlib.sha256(data).hexdigest() if data else None, "valid": bool(ok), "content_path": content_path})

    annex = {}
    for name, entries in by_name.items():
        m = ANNEX_RE.match(name)
        if not m:
            continue
        for e in entries:
            data = download(token, e["id"])
            ok = data is not None and hashlib.sha256(data).hexdigest() == m.group(2) and len(data) == int(m.group(1))
            annex.setdefault(name, []).append({"id": e["id"], "sha256": hashlib.sha256(data).hexdigest() if data else None, "valid": bool(ok)})

    manifest_list = []
    for name, entries in by_name.items():
        if not MANIFEST_RE.match(name):
            continue
        for e in entries:
            data = download(token, e["id"])
            sha = hashlib.sha256(data).hexdigest() if data is not None else None
            parsed, reasons, refs = False, [], []
            if data is not None:
                try:
                    lines = [ln.strip() for ln in data.decode("utf-8").splitlines() if ln.strip()]
                except UnicodeDecodeError:
                    lines = []
                    reasons.append("not-utf8")
                if lines and all(BUNDLE_RE.match(ln) for ln in lines):
                    parsed = True
                    refs = lines
                else:
                    if data is not None:
                        reasons.append("lines-not-bundle-names" if lines else "empty")
            bundles_valid = True
            if parsed:
                for ln in refs:
                    entries_b = by_name.get(ln, [])
                    ok = any(b["valid"] for b in bundles.get(ln, []))
                    if not entries_b:
                        bundles_valid = False
                        reasons.append(f"missing-bundle:{ln[:40]}…")
                    elif not ok:
                        bundles_valid = False
                        reasons.append(f"invalid-bundle:{ln[:40]}…")
            manifest_list.append({
                "id": e["id"], "name": name, "sha256": sha,
                "parsed": parsed, "bundles": refs,
                "bundles_valid": bool(parsed and bundles_valid),
                "invalid_reasons": reasons,
            })

    doc = {
        "folder_id": args.folder,
        "manifests": manifest_list,
        "bundles": bundles,
        "annex_objects": annex,
        "others": others,
    }
    with open(args.out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(f"snapshot folder={args.folder} manifests={len(manifest_list)} bundles={len(bundles)} annex={len(annex)} others={len(others)} out={args.out}")
    for mm in manifest_list:
        print(f"  manifest id={mm['id']} name={mm['name']} sha={mm['sha256'][:16] if mm['sha256'] else None} parsed={mm['parsed']} valid={mm['bundles_valid']} reasons={mm['invalid_reasons']}")


if __name__ == "__main__":
    main()
