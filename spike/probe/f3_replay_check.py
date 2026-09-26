#!/usr/bin/env python3
"""1.4f3：以「內容重放」判定同名的 GITMANIFEST 候選哪一個是真的（H1／H2）。

對 f3_snapshot.py 的 snapshot（依 file id 列舉並驗證）中的每個 manifest 候選：
- 依 manifest 內容列出的 bundle 名稱，逐一取「有效的」bundle 內容（snapshot 已驗證
  名稱與內容的 sha256/size 相符），依 manifest 順序 `git bundle unbundle` 到暫存 repo；
- 收集每個 bundle 宣告的 ref（去掉 git-remote-annex 的 namespace），後出現者覆蓋前者；
- 重放結果的 ref 集合與值必須完全等於 --expected-refs（例如 pending 或 promoted 的
  refs，含 main 與 git-annex）。

只有「所有引用 bundle 都有效」且「重放 refs 與期望完全相同」的候選會通過。
多個通過 → ambiguous（呼叫端必須 fail-closed）；零個通過 → fail。

用法：
  f3_replay_check.py --snapshot <snapshot.json> --expected-refs '{"refs/heads/main":"…","refs/heads/git-annex":"…"}' \
      [--out <result.json>] [--keep-workdir <dir>]
輸出（stdout）：每個候選一行 pass/fail 與原因；result.json 含 passing manifest ids。
需要本機 git；不碰 Drive 憑證（內容已由 snapshot 提供）。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

NS_RE = re.compile(r"^refs/namespaces/git-remote-annex/[0-9a-f-]+/")


def normalize(ref):
    return NS_RE.sub("", ref)


def unbundle_refs(scratch, bundle_path):
    r = subprocess.run(
        ["git", "-C", scratch, "bundle", "unbundle", bundle_path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return None, (r.stderr.strip().splitlines() or ["unbundle failed"])[-1]
    refs = {}
    for line in r.stdout.splitlines():
        parts = line.split(" ", 1)
        if len(parts) == 2:
            refs[normalize(parts[1].strip())] = parts[0].strip()
    return refs, None


def replay_manifest(scratch, workdir, m, bundles_meta):
    """回傳 (refs, problems)。"""
    problems = []
    if not m.get("parsed"):
        return None, ["manifest 無法解析"]
    refs = {}
    for bname in m.get("bundles", []):
        valid = [b for b in bundles_meta.get(bname, []) if b.get("valid")]
        if not valid:
            return None, [f"bundle 無有效實例：{bname[:48]}…"]
        bpath = valid[0].get("content_path") or os.path.join(workdir, f"bundle-{valid[0]['id']}.bin")
        if not os.path.exists(bpath):
            return None, [f"缺少 bundle 內容檔：{bpath}"]
        got, err = unbundle_refs(scratch, bpath)
        if got is None:
            return None, [f"unbundle 失敗：{bname[:48]}… ({err})"]
        refs.update(got)
    return refs, problems


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--snapshot", required=True)
    p.add_argument("--expected-refs", required=True)
    p.add_argument("--out", default=None)
    p.add_argument("--keep-workdir", default=None)
    p.add_argument("--resolve-trust", default=None,
                   help="模式：對每個非 .bak manifest 完整重放（不要求等於 expected-refs），"
                        "把「能重放且引用的 bundle 全部有效」的 manifest 內容雜湊寫到此檔（JSON 陣列）")
    args = p.parse_args()

    snap = json.load(open(args.snapshot))
    expected = json.loads(args.expected_refs)

    workdir = args.keep_workdir or tempfile.mkdtemp(prefix="f3-replay-")
    os.makedirs(workdir, exist_ok=True)
    scratch = os.path.join(workdir, "scratch")
    shutil.rmtree(scratch, ignore_errors=True)
    subprocess.run(["git", "init", "-q", "-b", "replay", scratch], check=True)

    bundles_meta = snap.get("bundles", {})

    if args.resolve_trust:
        trust = set()
        for m in snap.get("manifests", []):
            if m["name"].endswith(".bak"):
                continue
            refs, problems = replay_manifest(scratch, workdir, m, bundles_meta)
            if problems:
                print(f"resolve id={m['id']} name={m['name']} sha={(m.get('sha256') or '')[:16]}… "
                      f"NOT-TRUSTED ({'; '.join(problems)[:80]})")
                continue
            trust.add(m["sha256"])
            print(f"resolve id={m['id']} name={m['name']} sha={m['sha256'][:16]}… TRUSTED "
                  f"refs={json.dumps(refs, sort_keys=True)}")
        json.dump(sorted(trust), open(args.resolve_trust, "w"), indent=2)
        print(f"trust_written={args.resolve_trust} count={len(trust)}")
        return 0

    results = []
    for m in snap.get("manifests", []):
        mid, name = m["id"], m["name"]
        if name.endswith(".bak"):
            # `.bak` 是 git-remote-annex 的備份，內容依規則等於目前或上一個 manifest；
            # 候選判定只針對非 .bak 的 manifest，.bak 由 f3_clean 的內容規則處理。
            print(f"candidate id={mid} name={name} skipped (bak)")
            continue
        refs, problems = replay_manifest(scratch, workdir, m, bundles_meta)
        if refs is None:
            refs = {}
        ok = not problems and refs == expected
        if not problems and refs != expected:
            problems.append(f"重放 refs 不符：{sorted(refs)} != {sorted(expected)}")
        results.append({"id": mid, "name": name, "ok": ok, "problems": problems, "refs": refs,
                        "sha256": m.get("sha256")})
        print(f"candidate id={mid} name={name} ok={ok} refs={json.dumps(refs, sort_keys=True)}")
        for pr in problems:
            print(f"  problem: {pr}")

    passing = [r for r in results if r["ok"]]
    passing_shas = sorted({r["manifest_sha256"] for r in passing}) if all(
        "manifest_sha256" in r for r in results) else None
    # 內容去重：多個同內容副本視為同一個候選（副本由 f3_clean 去重隔離）
    by_sha = {}
    for r in passing:
        by_sha.setdefault(r.get("sha256"), []).append(r["id"])
    print(f"passing_count={len(passing)} distinct_passing_sha={len(by_sha)}")
    if len(by_sha) == 1:
        print("REPLAY-UNIQUE-PASS")
        rc = 0
    elif len(by_sha) == 0:
        print("REPLAY-NO-PASS: fail-closed")
        rc = 2
    else:
        print("REPLAY-AMBIGUOUS: fail-closed（多個不同內容的候選通過）")
        rc = 3

    if args.out:
        json.dump({"passing": passing[0]["id"] if len(by_sha) == 1 else None,
                   "passing_ids": [i for ids in by_sha.values() for i in ids],
                   "distinct_passing_shas": list(by_sha.keys()),
                   "expected_refs": expected, "results": results},
                  open(args.out, "w"), indent=2, sort_keys=True)
        print(f"result_written={args.out}")
    if not args.keep_workdir:
        shutil.rmtree(workdir, ignore_errors=True)
    return rc


if __name__ == "__main__":
    sys.exit(main())
