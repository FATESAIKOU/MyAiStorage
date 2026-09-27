#!/usr/bin/env bash
# 住民容器的啟動（Mac 端，tasks 5.1；group5-7 第 1.2 節）。
#
# 用法：
#   resident/run.sh <容器名> --profile mac-opencode --model <provider/model> [選項]
#
# 設計要點（D3、ADR 0006）：
# - **白名單**：秘密只從 ~/.config/aistorage/resident/<profile>/ 取，而且只有下列
#   檔名會以唯讀方式掛到 /secrets/：
#       rclone-worker.conf   同步器上傳收件匣（worker 的 drive.file 憑證）
#       sa-reader.json       讀取身分
#       signing.key          該 profile 的簽章私鑰
#       llm-<provider>.key   LLM 金鑰（**只掛 --model 指定的那個 provider**）
#       gh-pat-actions.txt   選用，5.3 觸發提交流程
#       reader.json          讀取設定（manifest id 等，不是秘密）
#   目錄裡出現任何不在白名單的檔名 → **拒絕啟動並報錯**（不是默默忽略）。
# - **工作目錄**每個容器一份：~/.local/share/aistorage/work/<容器名>/ → /work，
#   HOME=/work，容器彼此看不見（可以同時開多個）。
# - 不掛 docker.sock、不加 capability、不用 root（用 Mac 的 uid:gid；colima 的
#   sshfs 掛載需要這麼做，git-annex 才不會 chown 失敗）。
# - **不掛 Claude 的任何憑證**：白名單裡沒有，image 裡也沒有。
#
# 選項：
#   --profile <profile>     必填。決定秘密目錄與簽章金鑰所屬 profile。
#   --model <p/m>           必填。<provider>/<model>；金鑰取 llm-<provider>.key。
#                           依 PM 的隊員模型順序挑當下有額度的（不用 Claude）。
#   --no-tui                只啟動 serve 與同步器，不開互動介面（測試／CI）。
#   --publish-api [port]    把容器的 4096 埠發布到宿主機的 127.0.0.1（預設不發布），
#                           讓 Mac 上的 opencode 可以 attach。容器內 TUI 不需要。
#   --print-plan            只印出掛載計畫（JSON）後結束，不啟動容器。
#   --image <name>          image 名稱，預設 aistorage-resident:latest。
#
# 環境變數（測試或特殊情況）：
#   AISTORAGE_RESIDENT_ROOT     profile 秘密目錄的根，預設 ~/.config/aistorage/resident
#   AISTORAGE_WORK_ROOT         工作目錄根，預設 ~/.local/share/aistorage/work
#   AISTORAGE_RESIDENT_ALLOW_LOOSE_PERMS=1  允許權限過寬的秘密檔（不建議）
#   AISTORAGE_RESIDENT_USER     容器內 uid:gid，預設 "$(id -u):$(id -g)"
#   AISTORAGE_RESIDENT_PLATFORM docker --platform（例如 linux/amd64）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  sed -n '2,40p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

die() { echo "[run.sh] $*" >&2; exit 1; }

[ "$#" -ge 1 ] || usage
case "$1" in
  -h|--help) usage ;;
esac
name="$1"; shift

container_name_re='^[A-Za-z0-9][A-Za-z0-9_.-]*$'
if ! [[ "${name}" =~ ${container_name_re} ]]; then
  die "容器名不合法（允許 [A-Za-z0-9_.-]，且第一字元是英數）：${name}"
fi

profile=""
model=""
image="${AISTORAGE_RESIDENT_IMAGE:-aistorage-resident:latest}"
no_tui=0
print_plan=0
publish_port=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --profile) profile="${2:-}"; shift 2 ;;
    --model) model="${2:-}"; shift 2 ;;
    --image) image="${2:-}"; shift 2 ;;
    --publish-api)
      if [ -n "${2:-}" ] && [ "${2#-}" = "$2" ]; then publish_port="$2"; shift 2
      else publish_port="4096"; shift; fi
      ;;
    --no-tui) no_tui=1; shift ;;
    --print-plan) print_plan=1; shift ;;
    --) shift; break ;;
    -h|--help) usage ;;
    *) echo "未知的選項：$1" >&2; usage ;;
  esac
done
container_cmd=("$@")

[ -n "$profile" ] || die "需要 --profile（例如 mac-opencode）"
[ -n "$model" ] || die "需要 --model <provider/model>（依 PM 的隊員模型順序挑有額度的；不用 Claude）"
case "$model" in
  */*) : ;;
  *) die "--model 必須是 <provider>/<model>：${model}" ;;
esac
provider="${model%%/*}"
llm_key="llm-${provider}.key"

secrets_root="${AISTORAGE_RESIDENT_ROOT:-${HOME}/.config/aistorage/resident}"
work_root="${AISTORAGE_WORK_ROOT:-${HOME}/.local/share/aistorage/work}"
profile_dir="${secrets_root}/${profile}"

# ── 白名單：只有這些檔名能進 /secrets ─────────────────────────────────────
WHITELIST_REGEX='^(rclone-worker\.conf|sa-reader\.json|signing\.key|reader\.json|gh-pat-actions\.txt|llm-[a-z0-9][a-z0-9_-]*\.key)$'

[ -d "$profile_dir" ] || die "profile 秘密目錄不存在：${profile_dir}（建立它並放進白名單檔案）"

declare -a mount_args=() mounted=() violations=()
while IFS= read -r -d '' entry; do
  base="$(basename "$entry")"
  [ -n "$base" ] || continue
  if [[ ! "$base" =~ $WHITELIST_REGEX ]]; then
    violations+=("$base")
    continue
  fi
  [ -f "$entry" ] || die "秘密目錄裡有同名資料夾而不是檔案：${entry}"
  # 其他 provider 的金鑰可以存在，但**不掛**（最小權限）
  if [ "$base" != "$llm_key" ] && [[ "$base" == llm-*.key ]]; then
    continue
  fi
  if [ "${AISTORAGE_RESIDENT_ALLOW_LOOSE_PERMS:-0}" != "1" ]; then
    mode="$(stat -f '%Sp' "$entry" 2>/dev/null || stat -c '%A' "$entry")"
    group_other="$(printf '%s' "$mode" | cut -c5-10)"
    if printf '%s' "$group_other" | grep -q '[^ -]'; then
      die "拒絕掛載權限過寬的秘密檔（預期 0600 之類）：${base}（${mode}）→ chmod 600 '${entry}'"
    fi
  fi
  mount_args+=(--mount "type=bind,source=${entry},target=/secrets/${base},readonly")
  mounted+=("$base")
done < <(find "$profile_dir" -maxdepth 1 -mindepth 1 -print0)

if [ "${#violations[@]}" -gt 0 ]; then
  echo "[run.sh] 拒絕啟動：${profile_dir} 裡有不在白名單的檔案：" >&2
  printf '  - %s\n' "${violations[@]}" >&2
  cat >&2 <<'EOF'
  白名單只有：rclone-worker.conf、sa-reader.json、signing.key、reader.json、
              gh-pat-actions.txt、llm-<provider>.key
  （不掛 Claude 的任何憑證，也不掛其他 provider 的金鑰）
  要刻意掛某個檔案時，先把它從這個目錄移走，改由環境變數指向別處並自行評估。
EOF
  exit 1
fi

need() {
  local n
  for n in "$@"; do
    local found=0 m
    for m in "${mounted[@]:-}"; do [ "$m" = "$n" ] && found=1 && break; done
    [ "$found" = 1 ] || die "缺少必要檔案：${profile_dir}/${n}"
  done
}
need rclone-worker.conf signing.key "$llm_key"

work_dir="${work_root}/${name}"
mkdir -p "$work_dir"
chmod 700 "$work_dir"
mount_args+=(--mount "type=bind,source=${work_dir},target=/work")

container_user="${AISTORAGE_RESIDENT_USER:-$(id -u):$(id -g)}"

# ── 計畫（--print-plan；不啟動容器，輸出 JSON 供文件與測試使用）──────────
if [ "$print_plan" = 1 ]; then
  mounts_json=""
  for m in "${mounted[@]:-}"; do
    [ -n "$m" ] || continue
    mounts_json+="${mounts_json:+, }\"${m}\""
  done
  cat <<JSON
{
  "container": "${name}",
  "image": "${image}",
  "profile": "${profile}",
  "model": "${model}",
  "provider": "${provider}",
  "user": "${container_user}",
  "home": "/work",
  "work_dir": "${work_dir}",
  "secrets_dir": "${profile_dir}",
  "secrets_mounted": [${mounts_json}],
  "mounts": [
    {"source": "${work_dir}", "target": "/work", "readonly": false},
    {"source": "${profile_dir}/<name>", "target": "/secrets/<name>", "readonly": true}
  ],
  "publish_api": ${publish_port:-null},
  "tui": $([ "$no_tui" = 1 ] && echo false || echo true),
  "docker_sock": false,
  "privileged": false,
  "cap_add": []
}
JSON
  exit 0
fi

command -v docker >/dev/null 2>&1 || die "找不到 docker"
docker info >/dev/null 2>&1 || die "docker daemon 沒在跑（colima start）"
if docker inspect "$name" >/dev/null 2>&1; then
  die "已有同名容器存在：${name}（每個容器一份工作目錄，換名字或先 docker rm）"
fi
[ -d "$HERE/image" ] || die "找不到 image 建置檔：${HERE}/image"

echo "工作目錄：${work_dir} -> /work（HOME=/work）"
for m in "${mounted[@]:-}"; do
  [ -n "$m" ] || continue
  echo "掛載（唯讀）：${profile_dir}/${m} -> /secrets/${m}"
done
echo "啟動容器：${name}（image: ${image}，user: ${container_user}，model: ${model}）"

env_args=(
  -e HOME=/work
  -e AISTORAGE_MODEL="$model"
  -e AISTORAGE_PROVIDER="$provider"
  -e AISTORAGE_PROFILE="$profile"
)
[ "$no_tui" = 1 ] && env_args+=(-e AISTORAGE_RESIDENT_NO_TUI=1)

publish_args=()
if [ -n "$publish_port" ]; then
  publish_args=(-p "127.0.0.1:${publish_port}:4096")
  echo "發布 API 到宿主機 127.0.0.1:${publish_port}（attach 用的 port）"
fi

platform_args=()
if [ -n "${AISTORAGE_RESIDENT_PLATFORM:-}" ]; then
  platform_args=(--platform "${AISTORAGE_RESIDENT_PLATFORM}")
fi

tty_args=()
if [ "$no_tui" != 1 ] && [ -t 0 ] && [ -t 1 ]; then tty_args=(-it); fi

[ "${#container_cmd[@]}" -gt 0 ] || container_cmd=(bash -l)

exec docker run --rm --init --name "$name" ${tty_args[@]+"${tty_args[@]}"} \
  ${platform_args[@]+"${platform_args[@]}"} \
  --user "$container_user" \
  "${mount_args[@]}" \
  "${publish_args[@]+"${publish_args[@]}"}" \
  "${env_args[@]}" \
  -w /work \
  "$image" "${container_cmd[@]}"
