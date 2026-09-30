#!/usr/bin/env bash
# 技術驗證用的容器啟動腳本。
#
# 用法：
#   spike/env/run.sh <容器名> [秘密檔名...] [-- <容器內要執行的指令>]
#
# 行為：
#   - 只把「列出的」秘密檔（位於 ~/.config/aistorage-spike/）以唯讀掛到容器 /secrets/
#   - 每個容器一個專屬工作目錄，掛到 /work（預設
#     ~/.local/share/aistorage-spike/work/<容器名>/，彼此不共用）
#   - 不掛載任何其他的 home 目錄內容
#   - 秘密只以檔名引用；此腳本不讀、不印任何秘密檔內容
#
# colima 注意事項：
#   colima 的掛載是 sshfs，容器若以 root 跑，git-annex 會因為無法保留檔案
#   ownership（chown）而 push 失敗。這裡預設用 Mac 的 uid:gid 在容器內執行；
#   image 內的 git 已設 safe.directory=* 與預設身分，兩者搭配才能正常跑 git-annex。
#
# 環境變數（測試或特殊情況才需要）：
#   AISTORAGE_SPIKE_SECRETS        秘密目錄，預設 ~/.config/aistorage-spike
#   AISTORAGE_SPIKE_WORK_ROOT      工作目錄根，預設 ~/.local/share/aistorage-spike/work
#   AISTORAGE_SPIKE_IMAGE          image 名稱，預設 aistorage-spike-env:latest
#   AISTORAGE_SPIKE_ALLOW_LOOSE_PERMS=1  允許掛載權限過寬（group/other 可讀）的秘密檔
#   AISTORAGE_SPIKE_USER           容器內執行的 uid:gid，預設「Mac 的 $(id -u):$(id -g)」
#   AISTORAGE_SPIKE_PLATFORM       docker --platform（例如 linux/amd64）
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
用法：spike/env/run.sh <容器名> [秘密檔名...] [-- <指令>]

  容器名只能是 [A-Za-z0-9_.-]，用來當 docker --name 與工作目錄名。
  秘密檔名是 ~/.config/aistorage-spike/ 底下的檔名（不含路徑），可給多個。
  沒有列出的秘密不會進到容器；不給任何秘密檔名也可以。
  -- 之後的指令在容器內執行（預設 bash -l）。

  例：
    spike/env/run.sh s1
    spike/env/run.sh s1 rclone-mac-opencode.conf -- bash -lc 'rclone version'
    spike/env/run.sh s1 ollama-cloud-key.txt -- opencode
EOF
  exit 2
}

[ "$#" -ge 1 ] || usage
case "$1" in
  -h|--help) usage ;;
esac
name="$1"; shift

container_name_re='^[A-Za-z0-9][A-Za-z0-9_.-]*$'
if ! [[ "${name}" =~ ${container_name_re} ]]; then
  echo "容器名不合法（允許 [A-Za-z0-9_.-]）：${name}" >&2
  exit 2
fi

secrets_root="${AISTORAGE_SPIKE_SECRETS:-${HOME}/.config/aistorage-spike}"
work_root="${AISTORAGE_SPIKE_WORK_ROOT:-${HOME}/.local/share/aistorage-spike/work}"
image="${AISTORAGE_SPIKE_IMAGE:-aistorage-spike-env:latest}"

secret_names=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --) shift; break ;;
    -h|--help) usage ;;
    -*) echo "未知的選項：$1" >&2; usage ;;
    *) secret_names+=("$1"); shift ;;
  esac
done
container_cmd=("$@")

[ -d "${secrets_root}" ] || { echo "秘密目錄不存在：${secrets_root}" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "找不到 docker" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon 沒在跑（colima start）" >&2; exit 1; }

if docker inspect "${name}" >/dev/null 2>&1; then
  echo "已有同名容器存在：${name}（先移除或換名字）" >&2
  exit 1
fi

work_dir="${work_root}/${name}"
mkdir -p "${work_dir}"
chmod 700 "${work_dir}"

mount_args=(--mount "type=bind,source=${work_dir},target=/work")

seen=""
if [ "${#secret_names[@]}" -gt 0 ]; then
  for secret in "${secret_names[@]}"; do
    case "${secret}" in
      */*|..|.|'') echo "秘密檔名不合法（只能是檔名）：${secret}" >&2; exit 2 ;;
    esac

    case " ${seen} " in
      *" ${secret} "*) continue ;;
    esac
    seen="${seen} ${secret}"

    secret_path="${secrets_root}/${secret}"
    [ -f "${secret_path}" ] || { echo "秘密檔不存在或不是檔案：${secret_path}" >&2; exit 1; }

    if [ "${AISTORAGE_SPIKE_ALLOW_LOOSE_PERMS:-0}" != "1" ]; then
      mode="$(stat -f '%Sp' "${secret_path}")"
      group_other="$(printf '%s' "${mode}" | cut -c5-10)"
      if printf '%s' "${group_other}" | grep -q '[^ -]'; then
        echo "拒絕掛載權限過寬的秘密檔（預期 0600 之類）：${secret}（${mode}）" >&2
        echo "  修正：chmod 600 \"${secret_path}\"" >&2
        echo "  要略過檢查：AISTORAGE_SPIKE_ALLOW_LOOSE_PERMS=1" >&2
        exit 1
      fi
    fi

    mount_args+=(--mount "type=bind,source=${secret_path},target=/secrets/${secret},readonly")
    echo "掛載（唯讀）：${secret_path} -> /secrets/${secret}"
  done
fi

echo "工作目錄：${work_dir} -> /work"
container_user="${AISTORAGE_SPIKE_USER:-$(id -u):$(id -g)}"
echo "啟動容器：${name}（image: ${image}，user: ${container_user}）"

tty_args=()
if [ -t 0 ] && [ -t 1 ]; then tty_args=(-it); fi

if [ "${#container_cmd[@]}" -eq 0 ]; then
  container_cmd=(bash -l)
fi

platform_args=()
if [ -n "${AISTORAGE_SPIKE_PLATFORM:-}" ]; then
  platform_args=(--platform "${AISTORAGE_SPIKE_PLATFORM}")
fi

exec docker run --rm --init --name "${name}" ${tty_args[@]+"${tty_args[@]}"} \
  ${platform_args[@]+"${platform_args[@]}"} \
  --user "${container_user}" \
  "${mount_args[@]}" \
  -e HOME=/work -w /work \
  "${image}" "${container_cmd[@]}"
