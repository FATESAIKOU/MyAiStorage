#!/usr/bin/env bash
# 1.3 抹除驗證：佈置 canary、抹除、查驗。
# 用法：bash annex_1_3_erase.sh <part> <workdir> <prefix> <canary>
#   part = setup | erase-local | show-state | clone-verify
# 前置：RCLONE_CONFIG 指向 committer conf
set -euo pipefail

part="${1:?}"
workdir="${2:?}"
prefix="${3:?}"
canary="${4:?}"

export HOME="${HOME:-/work}"
log() { printf '\n### %s\n' "$*"; }

case "${part}" in
  setup)
    rm -rf "${workdir}"
    mkdir -p "${workdir}"
    cd "${workdir}"
    git init -q -b main .
    git annex init erase >/dev/null
    git annex initremote drive type=rclone encryption=none rcloneremotename=gdrive \
      rcloneprefix="${prefix}" autoenable=true --with-url >/dev/null
    git annex config --set annex.largefiles 'largerthan=100kb' >/dev/null

    log "1) canary v1（git 檔）"
    echo "canary v1: ${canary}" > erase-canary.txt
    git add erase-canary.txt
    git commit -qm "add canary file"
    git push drive main git-annex 2>&1 | tail -2

    log "2) canary v2（改寫同一檔，讓 v1 留在歷史）"
    echo "canary v2: ${canary} updated" > erase-canary.txt
    git commit -qam "update canary file"
    git push drive main git-annex 2>&1 | tail -2

    log "3) annex 物件內含 canary（>100KiB 進 annex）"
    python3 -c "
import sys
sys.stdout.write('canary in annex content: ' + sys.argv[1] + '\n' + 'PADDING ' * 24000)
" "${canary}" > big-annex.bin
    ls -la big-annex.bin
    git add big-annex.bin
    git commit -qm "add annex file with canary"
    git annex copy --to drive 2>&1 | tail -2
    git push drive main git-annex 2>&1 | tail -2

    log "4) 記錄完整 URL 與遠端狀態"
    uuid="$(git annex info drive --fast 2>/dev/null | awk '/^uuid:/{print $2}')"
    printf 'annex::%s?encryption=none&type=rclone&rcloneremotename=gdrive&rcloneprefix=%s\n' "${uuid}" "${prefix}" > "${workdir}.url"
    echo "uuid=${uuid}"
    echo "canary 存在於：erase-canary.txt（v1 歷史、v2 現行）、big-annex.bin（annex 物件）"
    echo "--- push 後 remote 物件 ---"
    rclone lsf --files-only --recursive "gdrive:${prefix}/" 2>/dev/null | sort
    ;;

  erase-local)
    cd "${workdir}"
    log "A) drop annex 內容（本地＋drive）；--force 因為沒有其他副本"
    git annex drop --force big-annex.bin 2>&1 | tail -2 || true
    git annex drop --force --from=drive big-annex.bin 2>&1 | tail -2 || true
    echo "--- whereis 現況（應無任何副本） ---"
    git annex whereis big-annex.bin 2>&1 | head -5 || true

    log "B) forget --force（抹掉 annex 狀態與歷史）"
    git annex forget --force 2>&1 | tail -5 || true

    log "B2) 清掉本地 annex 物件（filter-repo 不會碰 annex 物件；留著會在重建時被重新上傳）"
    chmod -R u+w .git/annex/objects 2>/dev/null || true
    rm -rf .git/annex/objects
    echo "   剩餘 annex 物件數：$(find .git/annex/objects -type f 2>/dev/null | wc -l | tr -d ' ')"

    log "C) filter-repo 改寫歷史（移除 canary 檔並替換字串）"
    # refs/annex/last-index 是 git-annex 的索引樹，會保留舊的 blob；filter-repo 不處理它
    git update-ref -d refs/annex/last-index 2>/dev/null || true
    printf 'literal:%s==>REDACTED\n' "${canary}" > /tmp/replacements.txt
    git filter-repo --force --replace-text /tmp/replacements.txt \
      --invert-paths --path erase-canary.txt --path big-annex.bin 2>&1 | tail -6 || true
    rm -f /tmp/replacements.txt
    # filter-repo 後 annex 分支可能被重建，再清一次 last-index 與殘留物件
    git update-ref -d refs/annex/last-index 2>/dev/null || true
    git reflog expire --expire=now --all 2>/dev/null || true
    git gc --prune=now --aggressive 2>/dev/null | tail -1 || true
    chmod -R u+w .git/annex/objects 2>/dev/null || true
    rm -rf .git/annex/objects

    log "D) 檢查改寫後的本機歷史與物件"
    echo "--- git log --all -S canary（應無輸出） ---"
    git log --all -S "${canary}" -p 2>&1 | head -3 || true
    echo "--- rev-list --all --objects 內 erase-canary.txt 的項數 ---"
    git rev-list --all --objects | grep -c 'erase-canary.txt' || echo 0
    echo "--- 全 repo 物件掃 canary（cat-file --batch-all-objects）---"
    git cat-file --batch-all-objects --batch 2>/dev/null | grep -cF "${canary}" || echo "0 命中"
    echo "--- 工作樹與 annex 目錄掃 canary ---"
    grep -rlF "${canary}" . --exclude-dir=.git 2>/dev/null || echo "  （工作樹無命中）"
    find .git/annex -type f 2>/dev/null | while read -r f; do grep -qF "${canary}" "$f" && echo "  annex 命中：$f"; done || true
    echo "--- 目前分支 ---"
    git branch -a

    log "E) force push（重推 bundle）"
    # filter-repo 可能把所有 commit 清成空而把 main 整條刪掉（本測試的 canary 就是全部內容）；
    # 這時重建一個空的 main，讓 repo 仍可用。
    if ! git show-ref --verify -q refs/heads/main; then
      echo "   main 已被清空；重建空的 main（erase 後 repo 仍可用）"
      git checkout -q --orphan main
      git rm -rq --cached . 2>/dev/null || true
      git commit -q --allow-empty -m "erase: repo rebuilt empty after data removal"
    fi
    git push --force drive main git-annex 2>&1 | tail -3

    log "F) 記下重推後的 remote 物件"
    rclone lsf --files-only --recursive "gdrive:${prefix}/" 2>/dev/null | sort
    ;;

  show-state)
    cd "${workdir}"
    git log --oneline --all | head -5
    git ls-remote drive 2>/dev/null
    ;;

  *)
    echo "unknown part: ${part}" >&2
    exit 2
    ;;
esac
