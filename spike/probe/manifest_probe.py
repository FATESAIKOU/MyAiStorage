#!/usr/bin/env python3
"""1.4i 的 manifest 原地更新與 SA 讀取延遲量測。

用 committer 憑證在指定資料夾建 manifest.json（記 file id），原地更新 N 次，
每次確認 (a) file id 不變 (b) 內容為最新 (c) 沒有同名重複檔；並在每次更新完成後
立刻用 SA 憑證（sa-reader.json，完整 drive scope）依 id 輪詢讀取，量測
「更新完成 → SA 讀到新版本」的延遲。

秘密處理：兩份憑證都只讀進記憶體，只放進 HTTP header；輸出只有 file id、雜湊、
狀態碼與秒數。不印 token 與 client secret。
"""

import argparse
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_probe import API, call, load_token, reason_of  # noqa: E402


def content_of(token, file_id):
    r = call("GET", f"{API}/files/{file_id}", token, params={"alt": "media"})
    return r


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--committer-cred", required=True)
    p.add_argument("--sa-cred", required=True)
    p.add_argument("--parent", required=True, help="manifest.json 要放的資料夾 id")
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--timeout", type=float, default=90)
    args = p.parse_args()

    ctoken, _ = load_token(args.committer_cred)
    stoken, snote = load_token(args.sa_cred)
    lines = []

    # 建 manifest.json v0（multipart：要帶 name 與 parents，否則會落在根目錄）
    import io as _io

    boundary = "probe" + os.urandom(8).hex()
    meta = {"name": "manifest.json", "parents": [args.parent]}
    payload0 = json.dumps({"generation": 0, "ts": time.time()}, sort_keys=True).encode()
    body = _io.BytesIO()
    body.write(f"--{boundary}\r\n".encode())
    body.write(b"Content-Type: application/json; charset=UTF-8\r\n\r\n")
    body.write(json.dumps(meta).encode())
    body.write(f"\r\n--{boundary}\r\n".encode())
    body.write(b"Content-Type: application/json\r\n\r\n")
    body.write(payload0)
    body.write(f"\r\n--{boundary}--\r\n".encode())
    r = call(
        "POST",
        "https://www.googleapis.com/upload/drive/v3/files",
        ctoken,
        params={"uploadType": "multipart", "fields": "id,name,md5Checksum,parents"},
        data=body.getvalue(),
        headers={"Content-Type": f"multipart/related; boundary={boundary}"},
    )
    if not r.ok:
        reason, msg = reason_of(r)
        lines.append(f"create.http={r.status_code} reason={reason} message={msg}")
        print("\n".join(lines))
        return 1
    manifest_id = r.json()["id"]
    lines.append(f"create.http={r.status_code}")
    lines.append(f"manifest.file_id={manifest_id}")
    lines.append(f"sa.scope={snote}")

    first_seen = {}
    for i in range(1, args.rounds + 1):
        payload = json.dumps(
            {"generation": i, "ts": time.time(), "nonce": os.urandom(6).hex()},
            sort_keys=True,
        ).encode()
        want = hashlib.sha256(payload).hexdigest()
        t0 = time.time()
        r = call(
            "PATCH",
            f"https://www.googleapis.com/upload/drive/v3/files/{manifest_id}",
            ctoken,
            params={"uploadType": "media", "fields": "id,name,md5Checksum"},
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        t_written = time.time()
        line = f"update{i}.http={r.status_code}"
        if not r.ok:
            reason, msg = reason_of(r)
            line += f" reason={reason} message={msg}"
        lines.append(line)
        lines.append(f"update{i}.file_id={manifest_id} want_sha256={want}")

        # (c) 同名重複檢查（committer 視角）
        rn = call(
            "GET",
            f"{API}/files",
            ctoken,
            params={
                "q": f"name='manifest.json' and '{args.parent}' in parents and trashed=false",
                "fields": "files(id,name)",
            },
        )
        lines.append(f"update{i}.samename_count={len(rn.json().get('files', [])) if rn.ok else 'ERR'}")

        # SA 依 id 輪詢
        seen = None
        t_seen = None
        attempts = 0
        while time.time() - t0 < args.timeout:
            attempts += 1
            rs = content_of(stoken, manifest_id)
            if rs.ok and hashlib.sha256(rs.content).hexdigest() == want:
                seen = hashlib.sha256(rs.content).hexdigest()
                t_seen = time.time()
                break
            time.sleep(0.5)
        if seen:
            lines.append(
                f"update{i}.sa_seen=yes latency_s={(t_seen - t_written):.2f} "
                f"read_http={rs.status_code} attempts={attempts}"
            )
        else:
            lines.append(
                f"update{i}.sa_seen=no latency_s=n/a "
                f"read_http={rs.status_code} attempts={attempts}"
            )
        first_seen[i] = t_seen

    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
