#!/usr/bin/env python3
"""spike/scripts/bench_2_6.py

tasks 2.6: 量測原始紀錄放 git 還是 annex
- Config A: 原始紀錄放 git (annex.largefiles='not (include=*.json)')
- Config B: 原始紀錄放 annex (annex.largefiles='include=*.json')
- 支援測試 annex.max-git-bundles (預設 10 或 30)
- 量測：push 耗時、ls-remote 驗證耗時、consolidate 耗時與上傳大小、Drive 檔案數與總位元組、全新 clone 耗時。
"""

import argparse
import configparser
import json
import os
from pathlib import Path
import random
import shutil
import string
import subprocess
import sys
import time
import urllib.parse
import urllib.request


def generate_random_chunk(min_kb=0.5, max_kb=8.0):
    size = int(random.uniform(min_kb, max_kb) * 1024)
    code_snippets = [
        "def process_data(records):\n    for r in records:\n        yield transform(r)\n",
        "SELECT id, session_id, created_at, status FROM sessions WHERE in_progress = 1 ORDER BY id DESC LIMIT 50;\n",
        "class WorkerPool:\n    def __init__(self, size=10):\n        self.size = size\n",
        "import hashlib\nh = hashlib.sha256(data).hexdigest()\nassert len(h) == 64\n",
        "for i in range(100):\n    log.info('processing step %d', i)\n",
    ]
    parts = []
    current = 0
    while current < size:
        if random.random() < 0.4:
            s = random.choice(code_snippets)
        else:
            words = [
                "".join(
                    random.choices(
                        string.ascii_letters, k=random.randint(3, 10)
                    )
                )
                for _ in range(20)
            ]
            s = " ".join(words) + "\n"
        parts.append(s)
        current += len(s.encode("utf-8"))
    return "".join(parts)


def init_session(session_id: str) -> dict:
    now_ms = int(time.time() * 1000)
    return {
        "info": {
            "id": session_id,
            "title": f"Session {session_id}",
            "created_at": now_ms,
            "updated_at": now_ms,
            "sync_count": 0,
        },
        "messages": [],
    }


def advance_session(session: dict) -> None:
    num_msgs = random.randint(1, 3)
    now_ms = int(time.time() * 1000)
    session["info"]["updated_at"] = now_ms
    session["info"]["sync_count"] = session["info"].get("sync_count", 0) + 1
    start_mid = len(session["messages"])
    for i in range(num_msgs):
        role = "user" if (start_mid + i) % 2 == 0 else "assistant"
        chunk = generate_random_chunk()
        msg = {
            "id": f"msg_{session['info']['id']}_{start_mid + i:04d}",
            "role": role,
            "time": {"created": now_ms},
            "parts": [{"type": "text", "text": chunk}],
        }
        if role == "assistant" and random.random() < 0.5:
            tool_output = (
                "Running compiler...\nBuild succeeded: 0 errors, 0 warnings.\n"
                * 3
            )
            msg["parts"].append({
                "type": "tool",
                "call": {"name": "compile", "input": "make -j4"},
                "state": {"status": "success", "output": tool_output},
            })
        session["messages"].append(msg)


def run_cmd(cmd, cwd=None, check=True, capture_output=True, env=None, retries=3):
    if env is None:
        env = os.environ.copy()
    last_err = ""
    for attempt in range(1, retries + 1):
        res = subprocess.run(
            cmd,
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=True,
            env=env,
        )
        if res.returncode == 0:
            return res
        last_err = res.stderr if capture_output else f"exit {res.returncode}"
        if attempt < retries:
            time.sleep(2 * attempt)
    if check:
        raise RuntimeError(f"Command failed ({res.returncode}) after {retries} attempts: {' '.join(cmd)}\n{last_err}")
    return res


def init_repo(workdir: Path, prefix: str, config_type: str, max_bundles: int) -> str:
    """初始化 git-annex repo 與 rclone special remote"""
    workdir.mkdir(parents=True, exist_ok=True)
    if not (workdir / ".git").exists():
        run_cmd(["git", "init", "-q", "-b", "main", "."], cwd=workdir)
        run_cmd(["git", "annex", "init", f"bench-{workdir.name}"], cwd=workdir)

    remotes = run_cmd(["git", "remote"], cwd=workdir).stdout.splitlines()
    if "drive" not in remotes:
        run_cmd(
            [
                "git",
                "annex",
                "initremote",
                "drive",
                "type=rclone",
                "encryption=none",
                "rcloneremotename=gdrive",
                f"rcloneprefix={prefix}",
                "autoenable=true",
                "--with-url",
            ],
            cwd=workdir,
        )

    # 設定 annex.largefiles
    if config_type.upper() == "A":
        run_cmd(
            ["git", "annex", "config", "--set", "annex.largefiles", "not (include=*.json)"],
            cwd=workdir,
        )
    else:
        run_cmd(
            ["git", "annex", "config", "--set", "annex.largefiles", "include=*.json"],
            cwd=workdir,
        )

    # 設定 annex.max-git-bundles
    run_cmd(["git", "config", "annex.max-git-bundles", str(max_bundles)], cwd=workdir)

    # 建立 README 初始 commit
    readme = workdir / "README.md"
    if not readme.exists():
        readme.write_text(f"# Benchmark Repo: {prefix}\nConfig: {config_type}\nMax bundles: {max_bundles}\n")
        run_cmd(["git", "add", "README.md"], cwd=workdir)
        run_cmd(["git", "commit", "-qm", f"init: {prefix}"], cwd=workdir)
        run_cmd(["git", "push", "drive", "main"], cwd=workdir)

    # 取得完整的 remote URL 供全新 clone 使用
    uuid = None
    info_res = run_cmd(["git", "annex", "info", "drive", "--fast"], cwd=workdir)
    for line in info_res.stdout.splitlines():
        if line.startswith("uuid:"):
            uuid = line.split()[1]
            break
    if not uuid:
        raise RuntimeError("Could not find drive uuid")

    complete_url = f"annex::{uuid}?encryption=none&type=rclone&rcloneremotename=gdrive&rcloneprefix={prefix}"
    url_file = workdir.parent / f"{workdir.name}.url"
    url_file.write_text(complete_url)
    return complete_url


def parse_manifest(prefix: str):
    """讀取遠端 GITMANIFEST，回傳 active_bundles 列表與 removed_bundles 列表"""
    cmd = ["rclone", "lsf", "--files-only", f"gdrive:{prefix}/"]
    res = run_cmd(cmd, check=False)
    if res.returncode != 0:
        return [], []
    manifest_files = [f for f in res.stdout.splitlines() if f.startswith("GITMANIFEST")]
    if not manifest_files:
        return [], []

    target_manifest = manifest_files[0]
    for mf in manifest_files:
        if not mf.endswith(".bak"):
            target_manifest = mf
            break

    cat_res = run_cmd(["rclone", "cat", f"gdrive:{prefix}/{target_manifest}"], check=False)
    if cat_res.returncode != 0:
        return [], []

    active = []
    removed = []
    for line in cat_res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("-"):
            removed.append(line.lstrip("- ").strip())
        elif line.startswith("GITBUNDLE"):
            active.append(line)
    return active, removed


def measure_drive_prefix(prefix: str):
    """透過 rclone 量測前綴下的檔案數與總位元組"""
    res = run_cmd(["rclone", "lsf", "-R", "--files-only", f"gdrive:{prefix}/"], check=False)
    if res.returncode != 0:
        return {"total_files": 0, "total_bytes": 0, "active_bundles_bytes": 0, "annex_bytes": 0}

    files = res.stdout.splitlines()
    total_files = len(files)

    size_res = run_cmd(["rclone", "size", "--json", f"gdrive:{prefix}/"], check=False)
    total_bytes = 0
    if size_res.returncode == 0:
        try:
            sj = json.loads(size_res.stdout)
            total_bytes = sj.get("bytes", 0)
        except Exception:
            pass

    active, removed = parse_manifest(prefix)
    active_bytes = 0
    for b in active:
        if "-s" in b:
            try:
                s_part = b.split("-s")[1].split("--")[0]
                active_bytes += int(s_part)
            except Exception:
                pass

    return {
        "total_files": total_files,
        "total_bytes": total_bytes,
        "active_bundles_count": len(active),
        "removed_bundles_count": len(removed),
        "active_bundles_bytes": active_bytes,
    }


def main():
    parser = argparse.ArgumentParser(description="Task 2.6 benchmark harness")
    parser.add_argument("--config", choices=["A", "B"], required=True, help="A: Git blob, B: Annex object")
    parser.add_argument("--workdir", type=Path, required=True, help="Local work repo directory")
    parser.add_argument("--prefix", type=str, required=True, help="Remote Drive prefix, e.g. agora-2.6git")
    parser.add_argument("--max-bundles", type=int, default=10, help="annex.max-git-bundles (10 or 30)")
    parser.add_argument("--rounds", type=int, default=200, help="Number of commit & push rounds")
    parser.add_argument("--num-sessions", type=int, default=10, help="Total number of sessions (default 10)")
    parser.add_argument("--pre-seed-syncs", type=int, default=150, help="Pre-seeded syncs per session before round 1")
    parser.add_argument("--output-tsv", type=Path, required=True, help="Path to write per-push TSV log")
    parser.add_argument("--output-json", type=Path, required=True, help="Path to write summary metrics JSON")
    args = parser.parse_args()

    os.environ["HOME"] = str(args.workdir.parent)
    random.seed(42)

    print(f"=== Initializing repo: Config {args.config}, prefix={args.prefix}, max_bundles={args.max_bundles} ===", flush=True)
    complete_url = init_repo(args.workdir, args.prefix, args.config, args.max_bundles)
    print(f"Repo initialized. Complete URL: {complete_url}", flush=True)

    sessions_dir = args.workdir / "sessions"
    sessions_dir.mkdir(exist_ok=True)

    # 初始化 10 個 Session 物件並預先推進至指定同步次數
    print(f"=== Initializing {args.num_sessions} sessions with {args.pre_seed_syncs} pre-seeded syncs each ===", flush=True)
    sessions = {}
    total_preseed_bytes = 0
    for i in range(1, args.num_sessions + 1):
        sid = f"ses_{i:02d}"
        s_file = sessions_dir / f"{sid}.json"
        if s_file.exists():
            try:
                sessions[sid] = json.loads(s_file.read_text())
            except Exception:
                sessions[sid] = init_session(sid)
        else:
            sessions[sid] = init_session(sid)
            for _ in range(args.pre_seed_syncs):
                advance_session(sessions[sid])
            data = json.dumps(sessions[sid], indent=2)
            s_file.write_text(data)
            total_preseed_bytes += len(data.encode("utf-8"))

    sample_size = len(json.dumps(sessions["ses_01"], indent=2).encode("utf-8")) / (1024 * 1024)
    print(f"Pre-seeded sessions ready. Sample ses_01 size: {sample_size:.2f} MB, Total 10 sessions: {total_preseed_bytes / (1024*1024):.2f} MB", flush=True)

    # Commit 初始 sessions
    run_cmd(["git", "add", "sessions/"], cwd=args.workdir)
    status_res = run_cmd(["git", "status", "--porcelain"], cwd=args.workdir)
    if status_res.stdout.strip():
        run_cmd(["git", "commit", "-qm", f"init sessions with {args.pre_seed_syncs} syncs"], cwd=args.workdir)
        if args.config == "B":
            print("Config B: copying initial pre-seeded sessions to drive...", flush=True)
            run_cmd(["git", "annex", "copy", "--to", "drive", "sessions/"], cwd=args.workdir)
        print("Pushing initial sessions...", flush=True)
        run_cmd(["git", "push", "drive", "main", "git-annex"], cwd=args.workdir)

    print(f"=== Starting benchmark loop: {args.rounds} rounds ===", flush=True)
    tsv_file = args.output_tsv
    tsv_file.parent.mkdir(parents=True, exist_ok=True)

    with open(tsv_file, "w") as f_tsv:
        f_tsv.write("round\tselected_sessions\tannex_copy_ms\tpush_ms\tverify_ms\ttotal_ms\tis_consolidate\tconsolidate_bytes\tactive_bundles\n")

    history = []
    session_ids = [f"ses_{i:02d}" for i in range(1, args.num_sessions + 1)]
    approx_active = 2

    for r in range(1, args.rounds + 1):
        # 隨機挑 1~4 個 Session 推進
        k = random.randint(1, 4)
        chosen = random.sample(session_ids, k)

        for sid in chosen:
            advance_session(sessions[sid])
            s_file = sessions_dir / f"{sid}.json"
            s_file.write_text(json.dumps(sessions[sid], indent=2))

        # Git add & commit
        run_cmd(["git", "add", "sessions/"], cwd=args.workdir)
        run_cmd(["git", "commit", "-qm", f"round {r}: advance {','.join(chosen)}"], cwd=args.workdir)

        # Config B: 先 copy annex 物件至 drive
        annex_copy_ms = 0
        if args.config == "B":
            t_ac0 = time.time()
            run_cmd(["git", "annex", "copy", "--to", "drive", "sessions/"], cwd=args.workdir)
            annex_copy_ms = int((time.time() - t_ac0) * 1000)

        # Push to drive
        t_push0 = time.time()
        push_res = run_cmd(["git", "push", "drive", "main", "git-annex"], cwd=args.workdir, check=False)
        t_push1 = time.time()
        push_ms = int((t_push1 - t_push0) * 1000)

        # ls-remote 驗證
        t_vr0 = time.time()
        run_cmd(["git", "ls-remote", "drive", "refs/heads/main"], cwd=args.workdir, check=False)
        t_vr1 = time.time()
        verify_ms = int((t_vr1 - t_vr0) * 1000)

        total_ms = annex_copy_ms + push_ms + verify_ms

        # 檢查 consolidate：只在推測接近或超過 max_bundles，或 push_ms 明顯增加時才查詢遠端 manifest
        is_consolidate = False
        consolidate_bytes = 0
        cur_active_count = approx_active + 1

        if cur_active_count >= args.max_bundles or push_ms > 45000 or (r % args.max_bundles in [0, 1, args.max_bundles - 1]):
            cur_active, cur_removed = parse_manifest(args.prefix)
            cur_active_count = len(cur_active)
            if cur_active_count <= 2 and approx_active >= args.max_bundles - 2:
                is_consolidate = True
                if cur_active:
                    for b in cur_active:
                        if "-s" in b:
                            try:
                                b_size = int(b.split("-s")[1].split("--")[0])
                                if b_size > consolidate_bytes:
                                    consolidate_bytes = b_size
                            except Exception:
                                pass

        approx_active = cur_active_count

        record = {
            "round": r,
            "selected_sessions": ",".join(chosen),
            "annex_copy_ms": annex_copy_ms,
            "push_ms": push_ms,
            "verify_ms": verify_ms,
            "total_ms": total_ms,
            "is_consolidate": is_consolidate,
            "consolidate_bytes": consolidate_bytes,
            "active_bundles": cur_active_count,
        }
        history.append(record)

        with open(tsv_file, "a") as f_tsv:
            f_tsv.write(
                f"{r}\t{record['selected_sessions']}\t{annex_copy_ms}\t{push_ms}\t{verify_ms}\t{total_ms}\t{is_consolidate}\t{consolidate_bytes}\t{cur_active_count}\n"
            )

        cons_tag = f" [CONSOLIDATE: {consolidate_bytes} bytes]" if is_consolidate else ""
        ac_tag = f" annex_copy={annex_copy_ms}ms" if args.config == "B" else ""
        print(f"Round {r:03d}/{args.rounds:03d}:{ac_tag} push={push_ms}ms verify={verify_ms}ms total={total_ms}ms active={cur_active_count}{cons_tag}", flush=True)

    # 執行完畢後的 Drive 統計
    print(f"=== Measuring final Drive stats for {args.prefix} ===", flush=True)
    drive_stats = measure_drive_prefix(args.prefix)
    print(f"=== Drive stats for {args.prefix}: {drive_stats} ===", flush=True)

    # 全新 Clone 測試
    clone_dir = args.workdir.parent / f"test-clone-{args.config}-{args.max_bundles}"
    if clone_dir.exists():
        run_cmd(["chmod", "-R", "u+w", str(clone_dir)], check=False)
        shutil.rmtree(clone_dir, ignore_errors=True)

    print(f"=== Measuring fresh clone from {complete_url} ===", flush=True)
    t_cl0 = time.time()
    run_cmd(["git", "clone", "-b", "main", complete_url, str(clone_dir)])
    t_cl1 = time.time()
    clone_history_ms = int((t_cl1 - t_cl0) * 1000)

    # 取得最新 raw records
    t_get0 = time.time()
    if args.config == "B":
        run_cmd(["git", "annex", "get", "sessions/"], cwd=clone_dir)
    t_get1 = time.time()
    get_raw_ms = int((t_get1 - t_get0) * 1000)

    total_clone_ms = clone_history_ms + get_raw_ms
    print(f"Clone history: {clone_history_ms}ms, Get raw: {get_raw_ms}ms, Total: {total_clone_ms}ms", flush=True)

    # 清理 clone 測試目錄
    run_cmd(["chmod", "-R", "u+w", str(clone_dir)], check=False)
    shutil.rmtree(clone_dir, ignore_errors=True)

    # 計算統計摘要
    push_times = [h["push_ms"] for h in history]
    verify_times = [h["verify_ms"] for h in history]
    total_times = [h["total_ms"] for h in history]
    consolidations = [h for h in history if h["is_consolidate"]]

    def percentile(lst, p):
        if not lst:
            return 0
        s = sorted(lst)
        k = (len(s) - 1) * p
        f = int(k)
        c = f + 1
        if c < len(s):
            return s[f] + (s[c] - s[f]) * (k - f)
        return s[f]

    summary = {
        "config": args.config,
        "max_bundles": args.max_bundles,
        "prefix": args.prefix,
        "rounds": args.rounds,
        "pre_seed_syncs": args.pre_seed_syncs,
        "push_ms": {
            "mean": sum(push_times) / len(push_times) if push_times else 0,
            "p50": percentile(push_times, 0.50),
            "p90": percentile(push_times, 0.90),
            "max": max(push_times) if push_times else 0,
            "min": min(push_times) if push_times else 0,
        },
        "total_push_with_verify_ms": {
            "mean": sum(total_times) / len(total_times) if total_times else 0,
            "p50": percentile(total_times, 0.50),
            "p90": percentile(total_times, 0.90),
            "max": max(total_times) if total_times else 0,
        },
        "consolidations": {
            "count": len(consolidations),
            "avg_ms": sum(c["total_ms"] for c in consolidations) / len(consolidations) if consolidations else 0,
            "avg_bytes": sum(c["consolidate_bytes"] for c in consolidations) / len(consolidations) if consolidations else 0,
            "details": consolidations,
        },
        "drive_stats": drive_stats,
        "fresh_clone": {
            "clone_history_ms": clone_history_ms,
            "get_raw_ms": get_raw_ms,
            "total_ms": total_clone_ms,
        },
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output_json, "w") as f_json:
        json.dump(summary, f_json, indent=2)

    print(f"=== Benchmark complete. Summary saved to {args.output_json} ===", flush=True)


if __name__ == "__main__":
    main()
