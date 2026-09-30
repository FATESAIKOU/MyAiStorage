#!/usr/bin/env bash
# 1.4f2 對策 A：clone 後驗證 tip 釘選（「HEAD != 釘選值 → 中止」）。
#
# 用法（容器內）：
#   verify_pinned_head.sh <annex-url-file> <clone-dir> <pinned-head>
#
# 行為：
#   1. git clone annex::<url> <clone-dir>
#   2. git -C <clone-dir> rev-parse HEAD
#   3. 與 <pinned-head> 比對；不同就印 PIN-MISMATCH 並 exit 2（呼叫端不得繼續 push）
#      clone 本身失敗（git-remote-annex 解析到偽造／同名）也 exit 2。
# 輸出不含秘密。
set -uo pipefail

url_file="${1:?usage: verify_pinned_head.sh <url-file> <clone-dir> <pinned-head>}"
clone_dir="${2:?}"
pinned="${3:?}"

url="$(cat "${url_file}")"
rm -rf "${clone_dir}"

git clone "${url}" "${clone_dir}" >/tmp/verify-clone.log 2>&1
clone_rc=$?
if [ "${clone_rc}" -ne 0 ]; then
  echo "PIN-CHECK: clone failed (rc=${clone_rc})"
  tail -3 /tmp/verify-clone.log | sed 's/^/  /'
  echo "PIN-MISMATCH: abort (clone 失敗視同釘選不符)"
  exit 2
fi

head="$(git -C "${clone_dir}" rev-parse HEAD)"
echo "pinned_head=${pinned}"
echo "cloned_head=${head}"
if [ "${head}" = "${pinned}" ]; then
  echo "PIN-OK"
  exit 0
fi
echo "PIN-MISMATCH: abort（不得繼續 push）"
exit 2
