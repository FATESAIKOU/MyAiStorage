#!/usr/bin/env bash
# 1.2 步驟 3〜5：建 repo、放小檔與大檔、push、clone、get、log/diff/revert、中斷測試。
# 在 arm64 容器內執行（一次性驗證容器掛 committer conf；非住民容器，見 1.2 evidence 備註）。
#
# 用法：bash /work/annex_1_2_core.sh <part> <workdir> <prefix>
#   part = core | interrupt
# 前置：RCLONE_CONFIG 指向 committer conf
set -euo pipefail

part="${1:?usage: annex_1_2_core.sh <part> <workdir> <prefix>}"
workdir="${2:?}"
prefix="${3:?}"
[ -n "${RCLONE_CONFIG:-}" ] || { echo "RCLONE_CONFIG 未設" >&2; exit 2; }

export HOME="${HOME:-/work}"
log() { printf '\n### %s\n' "$*"; }
annex_init() { git annex init "$1" >/dev/null 2>&1 || true; }
push_all() { git push drive main git-annex "$@"; }

case "${part}" in
  core)
    log "0) 環境版本"
    git annex version | head -1
    rclone version | head -1
    git --version

    log "1) 建 repo（${prefix}）＋小檔與大檔"
    bash /work/annex_repo_init.sh "${workdir}" "${prefix}"

    cd "${workdir}"
    echo "small-$(date -u +%s)" > small.txt
    dd if=/dev/urandom of=large.bin bs=1024 count=300 2>/dev/null
    sha256sum large.bin | tee large.sha256
    git add small.txt large.bin
    git commit -qm "add small + large"

    log "2) 上傳 annex 內容（copy --to drive；不改變 GITMANIFEST，可先做）"
    t0=$(date +%s%3N)
    git annex copy --to drive 2>&1 | tail -2
    t1=$(date +%s%3N)
    echo "annex_copy_ms=$((t1 - t0))"

    log "3) manifest 預檢（正向）＋量測『預檢結束→push 完成』窗口（M8）"
    bash /work/manifest_precheck.sh snapshot "${workdir}" "${prefix}"
    t0=$(date +%s%3N)
    bash /work/manifest_precheck.sh check "${workdir}" "${prefix}"
    t1=$(date +%s%3N)
    echo "precheck_read_ms=$((t1 - t0))"
    t2=$(date +%s%3N)
    push_all 2>&1 | tail -3
    t3=$(date +%s%3N)
    echo "unprotected_window_ms=$((t3 - t2))（預檢讀完到 push 寫完；D2 的競爭窗口）"

    log "3b) push 後重讀 manifest：確認指紋與檔案 id 的變化方式（決定預檢能不能以 id 比對）"
    bash /work/manifest_precheck.sh check "${workdir}" "${prefix}" \
      && echo "（預期外：push 後指紋竟然相同）" \
      || echo "（預期內：push 後指紋改變）"
    echo "--- remote 上的 GITMANIFEST 檔案與 id（push 後）---"
    rclone lsjson --files-only --recursive "gdrive:${prefix}/" --config "${RCLONE_CONFIG}" 2>/dev/null \
      | python3 -c 'import json,sys
for f in json.load(sys.stdin):
    if "GITMANIFEST" in f["Name"]:
        print("  " + f["ID"] + "  " + f["Name"])'

    log "4) 全新 clone（arm64）＋get＋雜湊比對"
    cd /work
    rm -rf "${workdir}-clone"
    t0=$(date +%s%3N)
    git clone "$(cat "${workdir}.url")" "${workdir}-clone" 2>&1 | tail -2
    t1=$(date +%s%3N)
    echo "clone_ms=$((t1 - t0))"
    cd "${workdir}-clone"
    annex_init clone
    echo "clone 內 remote:"; git remote -v
    git annex get large.bin
    sha256sum large.bin
    cmp large.bin "${workdir}/large.bin" && echo "large.bin 內容一致"

    log "5) log / diff"
    git log --oneline
    echo "diff HEAD~1..HEAD --stat:"; git diff HEAD~1..HEAD --stat

    log "6) revert 一次並 push revert（在 clone 內做；此處 remote 名為 origin）"
    git revert --no-edit HEAD
    git push origin main git-annex 2>&1 | tail -2

    log "7) 又一個全新 clone，比對 HEAD 與檔案"
    cd /work
    rm -rf "${workdir}-clone2"
    git clone "$(cat "${workdir}.url")" "${workdir}-clone2" 2>&1 | tail -2
    cd "${workdir}-clone2"
    annex_init clone2
    echo "HEAD=$(git rev-parse HEAD)"
    git log --oneline
    ls -la
    echo "(revert 掉 large.bin 後，工作樹沒有它＝預期)"
    ;;

  interrupt)
    log "8) 中斷測試：先同步（模擬真實寫入前先 pull），再做稍大的 commit"
    cd "${workdir}"
    git fetch drive main git-annex 2>&1 | tail -2
    git rebase drive/main 2>&1 | tail -2
    git annex config --set annex.largefiles 'largerthan=100kb' >/dev/null
    for i in 1 2 3; do dd if=/dev/urandom of="big-$i.bin" bs=1024 count=4096 2>/dev/null; done
    git add big-*.bin
    git commit -qm "big files for interrupt test"
    git annex copy --to drive 2>&1 | tail -1
    echo "--- 開始 push（3 秒後 SIGKILL）---"
    set +e
    timeout -s KILL 3 git push drive main git-annex
    rc=$?
    set -e
    echo "timeout rc=${rc}（137 = SIGKILL，代表真的中斷了）"
    echo "--- 中斷後立刻查 remote 上的物件（部分上傳／舊版本的痕跡）---"
    rclone lsf --files-only --recursive "gdrive:${prefix}/" 2>/dev/null | wc -l | sed 's/^/remote 檔案數：/'
    rclone lsf --files-only --recursive "gdrive:${prefix}/" 2>/dev/null | sort | sed 's/^/  /'

    log "9) 重新 push（先重讀遠端狀態；中斷後遠端可能已前進）"
    git fetch drive main git-annex 2>&1 | tail -2
    t0=$(date +%s%3N)
    set +e
    git push drive main git-annex 2>&1 | tail -4
    push_rc=${PIPESTATUS[0]}
    set -e
    t1=$(date +%s%3N)
    echo "re-push rc=${push_rc} ms=$((t1 - t0))"
    if [ "${push_rc}" -ne 0 ]; then
      echo "（非 fast-forward：遠端在中斷期間前進了；改以 rebase 後重推）"
      git rebase drive/main 2>&1 | tail -2
      git push drive main git-annex 2>&1 | tail -3
    fi

    log "10) 全新 clone 驗證中斷前已 commit 的內容都在（沒有遺失）"
    cd /work
    rm -rf "${workdir}-clone3"
    git clone "$(cat "${workdir}.url")" "${workdir}-clone3" 2>&1 | tail -2
    cd "${workdir}-clone3"
    annex_init clone3
    git log --oneline | head -3
    for i in 1 2 3; do
      git annex get "big-$i.bin"
      cmp "big-$i.bin" "${workdir}/big-$i.bin" && echo "big-$i.bin 一致"
    done
    ;;

  *)
    echo "unknown part: ${part}（core|interrupt）" >&2
    exit 2
    ;;
esac

echo
echo "### part=${part} 完成"
