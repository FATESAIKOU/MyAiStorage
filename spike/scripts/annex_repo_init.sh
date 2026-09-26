#!/usr/bin/env bash
# 建一個「git-annex repo + Drive 上的 rclone special remote + git-remote-annex remote」，
# 供技術驗證各子項使用（1.2 建 `agora-basic/`；1.3／1.5／1.8 各自用自己的前綴重跑）。
#
# 用法：
#   bash /work/annex_repo_init.sh <工作目錄> <rcloneprefix>
#
# 前置（容器內）：
#   - RCLONE_CONFIG 指向 committer conf（或任何對 SPIKE_FOLDER_ID 有寫入權的 conf）
#   - rclone 的 gdrive remote 名稱必須是 `gdrive`（git-remote-annex 的 URL 會記住這個名字）
#   - rclone conf 需已設 root_folder_id = SPIKE_FOLDER_ID（H6），prefix 一律用相對路徑
#
# 產出：
#   - repo 建在 <工作目錄>，已含一個 README commit 並推上 Drive
#   - <工作目錄>.url 記著 `git config remote.drive.url`（完整的 annex:: URL）
#   - stdout 不含任何秘密
set -euo pipefail

workdir="${1:?usage: annex_repo_init.sh <workdir> <rcloneprefix>}"
prefix="${2:?usage: annex_repo_init.sh <workdir> <rcloneprefix>}"

[ -n "${RCLONE_CONFIG:-}" ] || { echo "RCLONE_CONFIG 未設" >&2; exit 2; }
[ -r "${RCLONE_CONFIG}" ] || { echo "RCLONE_CONFIG 讀不到：${RCLONE_CONFIG}" >&2; exit 2; }

mkdir -p "${workdir}"
cd "${workdir}"

if [ -d .git ]; then
  echo "已存在 repo：${workdir}（不重做 init）" >&2
else
  git init -q -b main .
  git annex init "spike-$(basename "${workdir}")" >/dev/null
fi

if git remote get-url drive >/dev/null 2>&1; then
  echo "已設定 remote drive（不重做 initremote）" >&2
else
  # encryption=none：D5「依 annex key 直接取物件」的前提。
  # 不指定 chunk（預設不開）；之後以 annex info 驗證。
  git annex initremote drive \
    type=rclone \
    encryption=none \
    rcloneremotename=gdrive \
    rcloneprefix="${prefix}" \
    autoenable=true \
    --with-url
fi

# 大於 100 KiB 的進 annex；小檔留在 git
git annex config --set annex.largefiles 'largerthan=100kb' >/dev/null

echo "README for $workdir" > README.md
git add README.md
git commit -qm "init: ${prefix}"

echo "=== 確認 URL 由 --with-url 記進 git config ==="
shorthand_url="$(git config --get remote.drive.url)"
[ -n "${shorthand_url}" ] || { echo "remote.drive.url 是空的（--with-url 沒生效？）" >&2; exit 1; }

# 注意：git-annex 印出的「Full remote url」與 remote.drive.url 對 rclone remote 只含
# uuid/encryption/type，clone 時會缺 rcloneremotename／rcloneprefix 而失敗（實測）。
# 這裡以 remote.log 的參數組出完整 URL，供全新 clone 使用。
uuid="$(git annex info drive --fast 2>/dev/null | awk '/^uuid:/{print $2}')"
[ -n "${uuid}" ] || { echo "取不到 remote uuid" >&2; exit 1; }
complete_url="annex::${uuid}?encryption=none&type=rclone&rcloneremotename=gdrive&rcloneprefix=${prefix}"
printf '%s\n' "${complete_url}" > "${workdir}.url"
echo "uuid=${uuid}"

echo "=== 確認 remote 參數（不印秘密） ==="
git annex info drive --fast 2>/dev/null | grep -E '^(uuid|description|encryption|chunking|type):' || true

echo "=== push 初始版本 ==="
git push drive main 2>&1 | tail -3

echo "=== 摘要 ==="
echo "repo=${workdir}"
echo "prefix=${prefix}"
echo "url_file=${workdir}.url"
