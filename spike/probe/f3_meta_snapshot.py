#!/usr/bin/env python3
"""1.4f4（H4 前半）：只用 Drive metadata（sha256Checksum＋size）做列舉與驗證，
不下載 bundle 與 annex 物件。manifest 的「引用清單」只需要在需要重放時才下載
（--fetch-manifests 控制；預設只下載被釘選值信任的 manifest 來解析引用）。

輸出結構與 f3_snapshot.py 相同，但每個項目的 valid 由 metadata 判定：
- bundle：檔名內嵌 size/sha256 == metadata 的 size/sha256Checksum
- annex：檔名內嵌 size/sha256 == metadata 的 size/sha256Checksum
- manifest：metadata sha256Checksum == 釘選值（promoted／pending／prev）之一；
  trusted 的 manifest 一定下載（小檔）解析引用清單；非 trusted 的不下載。

用法：
  f3_meta_snapshot.py --cred <conf> --folder <id> --out <json> [--pin <json>] [--pending-pin <json>]
                      [--content-dir <dir>（需要重放時才會下載 bundle 內容）]
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_probe import API, call, load_token, reason_of  # noqa: E402

BUNDLE_RE = re.compile(r"^GITBUNDLE-s(\d+)--([0-9a-f-]+)-([0-9a-f]{64})$")
ANNEX_RE = re.compile(r"^SHA256E-s(\d+)--([0-9a-f]{64})(\..*)?$")
MANIFEST_RE = re.compile(r"^GITMANIFEST--[0-9a-f-]+(\.bak)?$")
MEDIA_GETS = {"count": 0, "bytes": 0}


def list_children_meta(token, folder_id):
    files, page = [], None
    while True:
        params = {
            "q": f"'{folder_id}' in parents and trashed=false",
            "fields": "nextPageToken,files(id,name,mimeType,size,md5Checksum,sha256Checksum)",
            "pageSize": 200,
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
    MEDIA_GETS["count"] += 1
    MEDIA_GETS["bytes"] += len(r.content)
    return r.content


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


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cred", required=True)
    p.add_argument("--folder", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--pin", default=None)
    p.add_argument("--pending-pin", default=None)
    p.add_argument("--content-dir", default=None)
    args = p.parse_args()

    token, _ = load_token(args.cred)
    manifest_shas, bundle_names = load_trust([args.pin, args.pending_pin])
    children = list_children_meta(token, args.folder)

    manifests, bundles, annex, others = [], {}, {}, []
    for f in children:
        name, fid = f["name"], f["id"]
        size = int(f["size"]) if f.get("size") else None
        sha = f.get("sha256Checksum")
        if f.get("mimeType") == "application/vnd.google-apps.folder":
            others.append({"id": fid, "name": name, "mimeType": f["mimeType"]})
        elif MANIFEST_RE.match(name):
            manifests.append({"id": fid, "name": name, "sha256": sha, "size": size})
        elif BUNDLE_RE.match(name):
            m = BUNDLE_RE.match(name)
            valid = sha == m.group(3) and size == int(m.group(1))
            bundles.setdefault(name, []).append({"id": fid, "sha256": sha, "size": size, "valid": valid})
        elif ANNEX_RE.match(name):
            m = ANNEX_RE.match(name)
            valid = sha == m.group(2) and size == int(m.group(1))
            annex.setdefault(name, []).append({"id": fid, "sha256": sha, "size": size, "valid": valid})
        else:
            others.append({"id": fid, "name": name, "mimeType": f["mimeType"]})

    # 只有被信任（內容雜湊在釘選值內）的 manifest 才下載解析引用清單；
    # 重放需要時（--content-dir）另外下載被引用的 bundle。
    manifest_list = []
    for mm in manifests:
        refs, parsed, content_sha = [], False, None
        if mm["sha256"] in manifest_shas:
            data = download(token, mm["id"])
            if data is not None:
                content_sha = __import__("hashlib").sha256(data).hexdigest()
                try:
                    lines = [ln.strip() for ln in data.decode("utf-8").splitlines() if ln.strip()]
                except UnicodeDecodeError:
                    lines = []
                if lines and all(BUNDLE_RE.match(ln) for ln in lines):
                    parsed = True
                    refs = lines
        bundles_valid = False
        if parsed:
            bundles_valid = all(
                any(b["valid"] for b in bundles.get(ln, [])) for ln in refs
            )
        if args.content_dir and refs:
            os.makedirs(args.content_dir, exist_ok=True)
            for ln in refs:
                for b in bundles.get(ln, []):
                    if b["valid"]:
                        path = os.path.join(args.content_dir, f"bundle-{b['id']}.bin")
                        if not os.path.exists(path):
                            data = download(token, b["id"])
                            if data is not None:
                                with open(path, "wb") as fh:
                                    fh.write(data)
                        b["content_path"] = path
                        break
        manifest_list.append({
            "id": mm["id"], "name": mm["name"], "sha256": mm["sha256"],
            "parsed": parsed, "bundles": refs, "bundles_valid": bool(bundles_valid),
            "trusted": mm["sha256"] in manifest_shas,
        })

    doc = {
        "folder_id": args.folder,
        "mode": "metadata-only",
        "manifests": manifest_list,
        "bundles": bundles,
        "annex_objects": annex,
        "others": others,
        "media_gets": dict(MEDIA_GETS),
    }
    with open(args.out, "w") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
    print(f"snapshot(meta) folder={args.folder} manifests={len(manifest_list)} bundles={len(bundles)} "
          f"annex={len(annex)} others={len(others)} media_gets={MEDIA_GETS['count']} bytes={MEDIA_GETS['bytes']}")
    for mm in manifest_list:
        print(f"  manifest id={mm['id']} name={mm['name']} sha={(mm['sha256'] or '')[:16]} "
              f"trusted={mm['trusted']} parsed={mm['parsed']} valid={mm['bundles_valid']}")


if __name__ == "__main__":
    main()
