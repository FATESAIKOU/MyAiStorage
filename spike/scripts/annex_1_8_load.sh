#!/usr/bin/env bash
# 1.8：本地 500 次 push 的量測（可續跑版）。
# - 以 tick-XXXX.txt 的 commit 為進度；重跑會從最後一個 commit 之後繼續
# - 每次 push 記錄耗時到 /work/load-push.tsv
# - 每 50 次記錄 GITBUNDLE 數與預檢窗口到 /work/load-metrics.tsv（review L5）
#
# 用法：bash annex_1_8_load.sh <workdir> <prefix> <max-push>
set -uo pipefail

workdir="${1:?}"; prefix="${2:?}"; max="${3:-500}"
export HOME="${HOME:-/work}"
[ -n "${RCLONE_CONFIG:-}" ] || { echo "RCLONE_CONFIG 未設" >&2; exit 2; }

cd "${workdir}"
# 增量 bundle 會讓每次 push 都要下載全部 active bundle（O(N)）；設 max-git-bundles 讓
# git-remote-annex 定期整庫重傳成單一 bundle，把成長壓成有界（review L5 要觀察這條）。
git config annex.max-git-bundles 10
tsv="${workdir}-push.tsv"; mtsv="${workdir}-metrics.tsv"
touch "${tsv}" "${mtsv}"

last_done=0
if ls tick-*.txt >/dev/null 2>&1; then
  last_done=$(ls tick-*.txt 2>/dev/null | sed 's/tick-0*//;s/\.txt//' | sort -n | tail -1)
  last_done=$((10#${last_done:-0}))
fi
echo "resume: last_done=${last_done} max=${max}"

for i in $(seq $((last_done + 1)) "${max}"); do
  f="tick-$(printf '%04d' "${i}").txt"
  echo "load commit ${i}" > "${f}"
  git add "${f}" >/dev/null
  git commit -qm "load ${i}"
  t0=$(date +%s%3N)
  git push drive main git-annex >/dev/null 2>&1
  rc=$?
  t1=$(date +%s%3N)
  printf '%s\t%s\t%s\n' "${i}" "$((t1 - t0))" "${rc}" >> "${tsv}"

  if [ $((i % 50)) -eq 0 ]; then
    bundles="$(rclone lsf --files-only "gdrive:${prefix}/" 2>/dev/null | grep -c '^GITBUNDLE' || true)"
    t2=$(date +%s%3N)
    bash /work/manifest_precheck.sh snapshot "${workdir}" "${prefix}" >/dev/null 2>&1
    t3=$(date +%s%3N)
    printf '%s\t%s\t%s\n' "${i}" "${bundles}" "$((t3 - t2))" >> "${mtsv}"
    echo "metric push=${i} bundles=${bundles} precheck_snapshot_ms=$((t3 - t2))"
  fi
done
echo "DONE max=${max}"
