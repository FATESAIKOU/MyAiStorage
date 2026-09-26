#!/usr/bin/env python3
"""1.4f3：提交流程的固定順序（模擬）——內容釘選、兩階段、自動恢復、全 ref 核對。

在 Mac 上執行；容器內的工具以 docker run 包裝（git-annex／rclone）。

子命令：
  snapshot   （Drive API）列舉＋內容驗證＋存 bundle 內容（給 replay 用）
  parents    （Drive API）多層前綴逐層同名檢查
  clean      （Drive API）內容判定遞迴清掃（移動失敗即非 0）
  verify     （容器）ls-remote 全部 ref + clone -b main + manifest 內容 → observed.json
  replay     （本機）內容重放，判定唯一 manifest
  prepare    （push 前）parents → 結算 pending（自動恢復）→ clean → verify 比對 promoted
  record-pending （push 前）以本地 ref 寫 pending
  after-push （push 後）verify refs == pending → snapshot → replay 唯一 → 轉正
"""

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
PY = sys.executable
CONF = os.path.expanduser("~/.config/aistorage-spike/rclone-committer.conf")
IMAGE = "aistorage-spike-env:latest"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def drive(cmd):
    return run([PY] + cmd)


def container(workdir, inner, name="oc14f3-run"):
    cmd = [
        "docker", "run", "--rm", "--init", "--name", name,
        "-u", f"{os.getuid()}:{os.getgid()}",
        "--mount", f"type=bind,source={workdir},target=/work",
        "-e", "HOME=/work", "-w", "/work",
        "-e", "RCLONE_CONFIG=/tmp/rclone.conf",
        "--mount", f"type=bind,source={CONF},target=/secrets/rclone-committer.conf,readonly",
        IMAGE, "bash", "-lc",
        "cp /secrets/rclone-committer.conf /tmp/rclone.conf && chmod 600 /tmp/rclone.conf && " + inner,
    ]
    return run(cmd)


def echo(r, ret_label=None):
    print(r.stdout, end="")
    sys.stderr.write(r.stderr)
    return r.returncode


def snapshot(a):
    out = os.path.join(a.workdir, "snapshot.json")
    content = os.path.join(a.workdir, "content")
    r = drive([os.path.join(ROOT, "spike/probe/f3_snapshot.py"),
               "--cred", CONF, "--folder", a.folder_id, "--out", out,
               "--content-dir", content])
    return echo(r)


def parents(a):
    r = drive([os.path.join(ROOT, "spike/probe/f3_parents.py"),
               "--cred", CONF, "--root", a.root_id, "--prefix", a.prefix,
               "--expected-ids", a.expected_ids, "--quarantine", a.quarantine] +
              (["--apply"] if a.apply else []))
    return echo(r)


def clean(a):
    r = drive([os.path.join(ROOT, "spike/probe/f3_clean.py"),
               "--cred", CONF, "--folder", a.folder_id, "--pin", a.pin,
               "--quarantine", a.quarantine] +
              (["--pending-pin", a.pending_pin] if os.path.exists(a.pending_pin) else []) +
              (["--extra-manifest-shas", a.extra_manifest_shas] if a.extra_manifest_shas else []) +
              (["--apply"] if a.apply else []))
    return echo(r)


def verify(a):
    sh = "/work/f3_verify_remote.sh"
    r = container(a.workdir, f"bash {sh} {a.url_file} verify-work {a.prefix}", name="oc14f3-verify")
    if r.returncode != 0:
        print("verify: remote unreadable (fail-closed)")
        sys.stderr.write(r.stderr)
        return 2
    with open(os.path.join(a.workdir, "observed.json"), "w") as fh:
        fh.write(r.stdout)
    obs = json.loads(r.stdout)
    print(f"verify: refs={json.dumps(obs['refs'], sort_keys=True)}")
    print(f"verify: manifest_sha256={obs['manifest_sha256'][:16]}… bundles={len(obs['manifest_bundles'])}")
    return 0


def replay(a, expected_refs, tag="replay"):
    out = os.path.join(a.workdir, f"{tag}-result.json")
    r = drive([os.path.join(ROOT, "spike/probe/f3_replay_check.py"),
               "--snapshot", os.path.join(a.workdir, "snapshot.json"),
               "--expected-refs", json.dumps(expected_refs, sort_keys=True),
               "--out", out])
    rc = echo(r)
    return rc, out


def promote_from_replay(a, pin, replay_result, refs):
    res = json.load(open(replay_result))
    mid = res.get("passing")
    if not mid:
        print("promote_refused: replay 沒有唯一候選")
        return 2
    snap = json.load(open(os.path.join(a.workdir, "snapshot.json")))
    cand = next(m for m in snap["manifests"] if m["id"] == mid)
    annex = {}
    for name, insts in snap.get("annex_objects", {}).items():
        valid = [i for i in insts if i.get("valid")]
        if valid:
            annex[name] = valid[0]["sha256"]
    prev_sha = None
    if os.path.exists(pin):
        try:
            prev_sha = json.load(open(pin)).get("manifest_sha256")
        except Exception:  # noqa: BLE001
            pass
    cmd = [os.path.join(ROOT, "spike/probe/f3_pin.py"), "promote", "--pin", pin,
           "--main-ref", refs["refs/heads/main"], "--annex-ref", refs["refs/heads/git-annex"],
           "--manifest-sha", cand["sha256"], "--manifest-name", cand["name"],
           "--bundles-json", json.dumps(cand["bundles"]), "--annex-json", json.dumps(annex)]
    if prev_sha and prev_sha != cand["sha256"]:
        cmd += ["--prev-manifest-sha", prev_sha]
    r = drive(cmd)
    return echo(r)


def load_pin(path):
    if os.path.exists(path):
        return json.load(open(path))
    return None


def settle_pending(a):
    """回傳 0=可繼續 2=中止。會依需要轉正或丟棄 pending。"""
    pending = load_pin(a.pending_pin)
    if pending is None:
        return 0
    promoted = load_pin(a.pin)
    print(f"settle: 發現 pending（main={pending['refs'].get('refs/heads/main')}）")
    rc = snapshot(a)
    if rc != 0:
        print("settle: snapshot 失敗 → 中止")
        return rc
    rc, rr = replay(a, pending["refs"], tag="settle-replay")
    if rc == 0:
        print("settle: pending 的 refs 可由遠端內容唯一重放 → 轉正（自動恢復）")
        rc = promote_from_replay(a, a.pin, rr, pending["refs"])
        if rc != 0:
            return rc
        os.remove(a.pending_pin)
        return 0
    if rc == 3:
        print("settle: 內容重放不唯一（疑似注入）→ fail-closed 中止本輪")
        return 2
    # rc == 2：遠端不是 pending 的內容 → 可能是 push 根本沒落地；比對 promoted
    rc = verify(a)
    if rc != 0:
        return rc
    obs = json.load(open(os.path.join(a.workdir, "observed.json")))
    if promoted and obs.get("refs") != promoted.get("refs"):
        # 遠端既非 pending 也非 promoted → 先清掃再看
        rc = clean(a)
        if rc != 0:
            return rc
        rc = verify(a)
        if rc != 0:
            return rc
        obs = json.load(open(os.path.join(a.workdir, "observed.json")))
    if promoted and obs.get("refs") == promoted.get("refs") and \
            obs.get("manifest_sha256") == promoted.get("manifest_sha256"):
        print("settle: 遠端等於 promoted → 丟棄未生效的 pending")
    else:
        print("settle: 遠端狀態不明 → fail-closed 中止")
        return 2
    os.remove(a.pending_pin)
    return 0


def cmd_check(a):
    """verify → 比對 promoted／pending，不清掃（測競速窗口的偵測）。"""
    rc = verify(a)
    if rc != 0:
        return rc
    obs = json.load(open(os.path.join(a.workdir, "observed.json")))
    promoted = load_pin(a.pin)
    pending = load_pin(a.pending_pin)

    def same(pin):
        return bool(pin) and obs.get("refs") == pin.get("refs") and \
            (pin.get("manifest_sha256") is None or obs.get("manifest_sha256") == pin.get("manifest_sha256"))

    if same(promoted):
        print("check: remote == promoted")
        return 0
    if same(pending):
        print("check: remote == pending（可轉正）")
        return 3
    print("check: remote 與 promoted/pending 皆不符 → fail-closed")
    if pending:
        print(f"  refs={json.dumps(obs.get('refs'), sort_keys=True)}")
        print(f"  pending_refs={json.dumps(pending.get('refs'), sort_keys=True)}")
        print(f"  promoted_refs={json.dumps(promoted.get('refs'), sort_keys=True) if promoted else None}")
    return 2


def cmd_prepare(a):
    print("== parents ==")
    rc = parents(a)
    if rc != 0:
        return rc
    print("== settle pending ==")
    rc = settle_pending(a)
    if rc != 0:
        return rc
    print("== clean ==")
    rc = clean(a)
    if rc != 0:
        return rc
    print("== verify vs promoted ==")
    rc = verify(a)
    if rc != 0:
        return rc
    obs = json.load(open(os.path.join(a.workdir, "observed.json")))
    promoted = load_pin(a.pin)
    if not promoted:
        print("prepare: 沒有 promoted 釘選值（首次初始化請直接 after-push）")
        return 0 if a.first_init else 2
    if obs.get("refs") != promoted.get("refs"):
        print(f"prepare: refs 不符 promoted（{obs.get('refs')} != {promoted.get('refs')}）")
        return 2
    if obs.get("manifest_sha256") != promoted.get("manifest_sha256"):
        print("prepare: manifest 內容 sha 不符 promoted → fail-closed")
        return 2
    print("prepare: OK")
    return 0


def cmd_record_pending(a):
    refs = json.loads(a.refs_json)
    r = drive([os.path.join(ROOT, "spike/probe/f3_pin.py"), "record-pending",
               "--pin", a.pending_pin,
               "--main-ref", refs["refs/heads/main"],
               "--annex-ref", refs["refs/heads/git-annex"]])
    return echo(r)


def cmd_after_push(a):
    print("== after-push: verify refs ===")
    rc = verify(a)
    if rc != 0:
        return rc
    obs = json.load(open(os.path.join(a.workdir, "observed.json")))
    pending = load_pin(a.pending_pin)
    if not pending:
        print("after-push: 沒有 pending 釘選值")
        return 2
    if obs.get("refs") != pending.get("refs"):
        print("after-push: refs != pending → fail-closed")
        return 2
    print("== after-push: snapshot ==")
    rc = snapshot(a)
    if rc != 0:
        return rc
    print("== after-push: replay ==")
    rc, rr = replay(a, pending["refs"], tag="after-push-replay")
    if rc != 0:
        print("after-push: 重放不唯一 → fail-closed（不轉正）")
        return rc
    rc = promote_from_replay(a, a.pin, rr, pending["refs"])
    if rc != 0:
        return rc
    os.remove(a.pending_pin)
    print("after-push: 已驗證並轉正")
    return 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument("cmd", choices=["snapshot", "parents", "clean", "verify", "replay",
                                   "prepare", "record-pending", "after-push", "check"])
    p.add_argument("--workdir", required=True)
    p.add_argument("--folder-id", default=None)
    p.add_argument("--root-id", default=None)
    p.add_argument("--prefix", default=None)
    p.add_argument("--expected-ids", default=None)
    p.add_argument("--quarantine", default=None)
    p.add_argument("--url-file", default=None)
    p.add_argument("--pin", default=None)
    p.add_argument("--pending-pin", default=None)
    p.add_argument("--extra-manifest-shas", default=None)
    p.add_argument("--refs-json", default=None)
    p.add_argument("--apply", action="store_true")
    p.add_argument("--first-init", action="store_true")
    a = p.parse_args()

    if a.pin is None:
        a.pin = os.path.join(a.workdir, "pin.json")
    if a.pending_pin is None:
        a.pending_pin = os.path.join(a.workdir, "pin.pending.json")

    if a.cmd == "snapshot":
        sys.exit(snapshot(a))
    if a.cmd == "parents":
        sys.exit(parents(a))
    if a.cmd == "clean":
        sys.exit(clean(a))
    if a.cmd == "verify":
        sys.exit(verify(a))
    if a.cmd == "check":
        sys.exit(cmd_check(a))
    if a.cmd == "record-pending":
        sys.exit(cmd_record_pending(a))
    if a.cmd == "replay":
        rc = snapshot(a)
        if rc != 0:
            sys.exit(rc)
        rc, _ = replay(a, json.loads(a.refs_json), tag="replay")
        sys.exit(rc)
    if a.cmd == "prepare":
        sys.exit(cmd_prepare(a))
    if a.cmd == "after-push":
        sys.exit(cmd_after_push(a))


if __name__ == "__main__":
    main()
