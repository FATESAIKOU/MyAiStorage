#!/usr/bin/env bash
# 1.4f3：遠端驗證（M1／H2）——clone -b main、全部 ref、manifest 內容與 annex 物件，
# 輸出 JSON 供 f3_pin.py check 比對。
#
# 用法（容器內）：
#   f3_verify_remote.sh <url-file> <workdir> <prefix>
# 產出 <workdir>/observed.json：
#   {
#     "refs": {"refs/heads/main": "<hash>", ...},   # git ls-remote 的全部 ref
#     "manifest_sha256": "...",                      # rclone cat 的 manifest 內容 sha256
#     "manifest_bundles": ["GITBUNDLE-…", ...],
#     "annex_objects": {"<name>": "<sha256>"},       # 前綴資料夾內 annex 物件（遞迴一層）
#     "clone_head": "<hash>", "clone_rc": 0
#   }
# 任何一步失敗 → exit 2（fail-closed）。log 一律 mktemp；錯誤只印類別。
set -uo pipefail

url_file="${1:?usage: f3_verify_remote.sh <url-file> <workdir> <prefix>}"
workdir="${2:?}"
prefix="${3:?}"
prefix="${prefix%/}"
mkdir -p "${workdir}"
url="$(cat "${url_file}")"

log="$(mktemp)"
trap 'rm -f "${log}"' EXIT

echo "{"
printf '  "generated_at": "%s",\n' "$(date -u +%FT%TZ)"

# 0) ls-remote 要在 git repo 內才有 remote helper（否則 "Not in a git repository"）
scratch="${workdir}/ls-remote-scratch"
rm -rf "${scratch}"
mkdir -p "${scratch}"
git init -q -b scratch "${scratch}" >>"${log}" 2>&1

# 1) ls-remote：全部 ref
refs_json="$(git -C "${scratch}" ls-remote "${url}" 2>>"${log}" | python3 -c '
import json,sys
refs={}
for line in sys.stdin:
    line=line.strip()
    if not line: continue
    parts=line.split("\t")
    if len(parts)!=2: continue
    refs[parts[1]]=parts[0]
print(json.dumps(refs,sort_keys=True))
')"
if [ -z "${refs_json}" ]; then
  echo "verify: ls-remote failed"
  tail -2 "${log}" | sed "s/^/  /" >&2
  exit 2
fi
printf '  "refs": %s,\n' "${refs_json}"

# 2) clone -b main（明確 ref；不依賴 HEAD）
clone_dir="${workdir}/verify-clone"
rm -rf "${clone_dir}"
if ! git clone -b main --single-branch "${url}" "${clone_dir}" >>"${log}" 2>&1; then
  echo "verify: clone -b main failed"
  exit 2
fi
clone_head="$(git -C "${clone_dir}" rev-parse refs/remotes/origin/main 2>>"${log}")"
printf '  "clone_head": "%s",\n' "${clone_head}"
printf '  "clone_rc": 0,\n'

# 3) manifest 內容（rclone cat，相對於 prefix）
manifest_name="$(rclone lsf --files-only "gdrive:${prefix}" 2>>"${log}" | grep -E '^GITMANIFEST--[0-9a-f-]+$' | head -1)"
if [ -z "${manifest_name}" ]; then
  echo "verify: no manifest found"
  exit 2
fi
manifest_sha="$(rclone cat "gdrive:${prefix}/${manifest_name}" 2>>"${log}" | sha256sum | cut -d' ' -f1)"
bundles_json="$(rclone cat "gdrive:${prefix}/${manifest_name}" 2>>"${log}" | python3 -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin if l.strip()],sort_keys=True))')"
printf '  "manifest_name": "%s",\n' "${manifest_name}"
printf '  "manifest_sha256": "%s",\n' "${manifest_sha}"
printf '  "manifest_bundles": %s,\n' "${bundles_json}"

# 4) annex 物件（前綴資料夾直接子項；SHA256E-*；sha256 由檔名推導，
#    內容是否相符由 f3_clean 逐檔驗證）
annex_final="$(rclone lsjson --files-only "gdrive:${prefix}" 2>>"${log}" | python3 -c '
import json,sys
files=json.load(sys.stdin)
out={}
for f in files:
    n=f["Name"]
    if n.startswith("SHA256E-") and "--" in n:
        out[n]=n.split("--")[1].split(".")[0]
print(json.dumps(out,sort_keys=True))
')"
printf '  "annex_objects": %s\n' "${annex_final}"
echo "}"
exit 0
