#!/usr/bin/env bash
# 1.2／1.5 共用的「manifest 預檢」實作（M8）。
#
# 用途：push 前重讀遠端 manifest，與 clone 時記下的指紋比對；不同就中止（D2）。
# 指紋：special remote 上所有 GITMANIFEST* 物件的內容 sha256（名稱＋雜湊一起比）。
#
# 用法：
#   bash /work/manifest_precheck.sh snapshot <repo> <rcloneprefix>
#   bash /work/manifest_precheck.sh check    <repo> <rcloneprefix>
#
# 指紋存 <repo>/.git/annex-manifest-snapshot（不進 commit）。
# exit：0=相同／snapshot 成功；3=不同（呼叫端必須中止 push）；2=用法、環境或 rclone 錯誤（fail-closed）。
#
# fail-closed 規則（review 1.2 M1）：
#   - rclone lsf／hashsum 失敗（網路、配額、權限等）一律 exit 2，不寫出空指紋。
#   - 錯誤只記「類別」（not-found／quota／auth／permission／network／other），
#     原始 stderr 只進暫存檔、不印出（避免任何內容外洩）。
set -euo pipefail

op="${1:?usage: manifest_precheck.sh <snapshot|check> <repo> <rcloneprefix>}"
repo="${2:?usage: manifest_precheck.sh <snapshot|check> <repo> <rcloneprefix>}"
prefix="${3:?usage: manifest_precheck.sh <snapshot|check> <repo> <rcloneprefix>}"
[ -n "${RCLONE_CONFIG:-}" ] || { echo "RCLONE_CONFIG 未設" >&2; exit 2; }

snap="${repo}/.git/annex-manifest-snapshot"
tmp="$(mktemp)"; err="$(mktemp)"
trap 'rm -f "${tmp}" "${tmp}.list" "${tmp}.lsf" "${err}"' EXIT

classify_rclone_error() {
  # 只回報類別；原始錯誤內容不輸出。
  local f="$1"
  if grep -qiE 'directory not found|object not found|not found|404' "${f}"; then echo 'not-found(404)'
  elif grep -qiE 'rateLimitExceeded|userRateLimitExceeded|quota|429' "${f}"; then echo 'quota-or-rate-limit'
  elif grep -qiE '401|unauthorized|invalid_grant|invalid credential' "${f}"; then echo 'auth(401)'
  elif grep -qiE '403|forbidden|permission|insufficient' "${f}"; then echo 'permission(403)'
  elif grep -qiE 'timeout|connection|dial|resolve|dns|network|unreachable|EOF|TLS' "${f}"; then echo 'network'
  else echo 'other'
  fi
}

if ! rclone lsf --files-only --recursive "gdrive:${prefix}/" >"${tmp}.lsf" 2>"${err}"; then
  echo "manifest_precheck: rclone lsf 失敗（類別：$(classify_rclone_error "${err}")）——中止" >&2
  exit 2
fi
grep 'GITMANIFEST' "${tmp}.lsf" | sort > "${tmp}.list" || true

if [ ! -s "${tmp}.list" ]; then
  echo "manifest_precheck: remote 上找不到 GITMANIFEST（prefix=${prefix}）" >&2
  exit 2
fi

: > "${tmp}"
while IFS= read -r rel; do
  if ! sum="$(rclone hashsum sha256 "gdrive:${prefix}/${rel}" 2>"${err}" | awk '{print $1}')"; then
    echo "manifest_precheck: 取 ${rel} 的 sha256 失敗（類別：$(classify_rclone_error "${err}")）——中止" >&2
    exit 2
  fi
  if [ -z "${sum}" ]; then
    echo "manifest_precheck: ${rel} 的 sha256 是空的（類別：$(classify_rclone_error "${err}")）——中止（fail-closed）" >&2
    exit 2
  fi
  printf '%s  %s\n' "${sum}" "${rel}" >> "${tmp}"
done < "${tmp}.list"
sort -o "${tmp}" "${tmp}"

case "${op}" in
  snapshot)
    cp "${tmp}" "${snap}"
    echo "snapshot ok：$(wc -l < "${snap}" | tr -d ' ') 個 manifest 物件"
    ;;
  check)
    [ -f "${snap}" ] || { echo "manifest_precheck: 沒有快照（先跑 snapshot）" >&2; exit 2; }
    if diff -q "${snap}" "${tmp}" >/dev/null; then
      echo "manifest_precheck: PASS（與快照相同，允許 push）"
    else
      echo "manifest_precheck: CHANGED（遠端 manifest 與快照不同）——中止 push" >&2
      diff "${snap}" "${tmp}" | sed 's/^/  /' >&2 || true
      exit 3
    fi
    ;;
  *)
    echo "unknown op: ${op}" >&2
    exit 2
    ;;
esac
