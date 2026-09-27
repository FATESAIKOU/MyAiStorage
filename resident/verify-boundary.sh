#!/usr/bin/env bash
# 在「實際的住民容器」裡錄一次能力邊界（tasks 5.1 驗收；group5-7 第 1.4 節）。
#
# 用法（容器內）：
#   resident/verify-boundary.sh            # 印出報告
#   resident/verify-boundary.sh --json     # 印出 JSON（方便存檔比對）
#
# 紀錄的內容（技術驗證 1.7a G6 的要求）：
#   1. 環境變數的**名稱**（不是值）
#   2. 完整的 /proc/mounts（過濾出 /secrets 與 /work）
#   3. docker.sock 是否可達
#   4. capability（CapEff）
#   5. ls /Users（應該看不到宿主機的使用者家目錄）
#   6. 寫入 /secrets 必須失敗
#   7. 秘密的值**不在** /work 裡（以 grep -cFf 比對，只輸出計數）
#   8. 沒有 Claude 的憑證（環境變數與檔案）
#
# 秘密只以路徑引用：本腳本不印任何秘密的內容，比對只輸出計數。
set -uo pipefail

as_json=0
[ "${1:-}" = "--json" ] && as_json=1

SECRETS_DIR="${AISTORAGE_SECRETS_DIR:-/secrets}"
WORK_DIR="${AISTORAGE_WORK_ROOT:-/work}"
fail=0
note() { [ "$as_json" = 1 ] || printf '%s\n' "$*"; }
bad() { echo "✗ $*" >&2; fail=1; }
ok() { [ "$as_json" = 1 ] || printf '✓ %s\n' "$*"; }

# ── 1. 環境變數名稱（不印值）───────────────────────────────────────────
env_names="$(env | sed 's/=.*//' | sort | tr '\n' ' ')"
# ── 2. 掛載（只留 /secrets 與 /work）───────────────────────────────────
mounts="$(grep -E ' (/secrets|/work) ' /proc/mounts 2>/dev/null || true)"
secrets_mount_count="$(printf '%s\n' "$mounts" | grep -c ' /secrets' || true)"
work_mount_count="$(printf '%s\n' "$mounts" | grep -c ' /work' || true)"
# ── 3. docker.sock ─────────────────────────────────────────────────────
docker_sock="absent"
[ -S /var/run/docker.sock ] && docker_sock="PRESENT"
# ── 4. capability ──────────────────────────────────────────────────────
cap_eff="$(awk '/^CapEff:/{print $2}' /proc/self/status 2>/dev/null || echo unknown)"
# ── 5. 宿主機家目錄 ────────────────────────────────────────────────────
users_visible="$(ls -1 /Users 2>/dev/null | tr '\n' ' ')"
# ── 6. /secrets 可寫？─────────────────────────────────────────────────
secrets_writable="unknown"
if ( : > "${SECRETS_DIR}/.boundary-probe" ) 2>/dev/null; then
  secrets_writable="WRITABLE"
  rm -f "${SECRETS_DIR}/.boundary-probe" 2>/dev/null || true
else
  secrets_writable="read-only"
fi
# ── 7. 秘密的值不在 /work 裡 ──────────────────────────────────────────
leak_total=0
leak_detail=""
for f in "${SECRETS_DIR}"/*; do
  [ -f "$f" ] || continue
  # 檔案太小（<8 bytes）容易誤判，跳過
  size="$(wc -c < "$f" | tr -d ' ')"
  [ "$size" -ge 8 ] || continue
  count="$(grep -r -c -F -f "$f" "$WORK_DIR" 2>/dev/null | awk -F: '$NF>0{n++} END{print n+0}')"
  leak_total=$((leak_total + count))
  leak_detail="${leak_detail}$(basename "$f")=${count} "
done
# ── 8. Claude 憑證 ─────────────────────────────────────────────────────
claude_env="$(env | sed 's/=.*//' | grep -E '^(ANTHROPIC|CLAUDE)' | tr '\n' ' ')"
claude_files=""
for p in /work/.claude.json /work/.claude /work/.config/anthropic \
         /work/.config/opencode/auth.json /work/.local/share/opencode/auth.json; do
  [ -e "$p" ] && claude_files="${claude_files}${p} "
done

if [ "$as_json" = 1 ]; then
  cat <<JSON
{
  "env_names": "$(printf '%s' "$env_names" | sed 's/"/\\"/g')",
  "secrets_mount_count": ${secrets_mount_count:-0},
  "work_mount_count": ${work_mount_count:-0},
  "mounts": "$(printf '%s' "$mounts" | tr '\n' ';' | sed 's/"/\\"/g')",
  "docker_sock": "${docker_sock}",
  "cap_eff": "${cap_eff}",
  "users_visible": "$(printf '%s' "$users_visible" | sed 's/"/\\"/g')",
  "secrets_writable": "${secrets_writable}",
  "secret_leak_hits": ${leak_total},
  "secret_leak_detail": "${leak_detail}",
  "claude_env": "$(printf '%s' "$claude_env" | sed 's/"/\\"/g')",
  "claude_files": "$(printf '%s' "$claude_files" | sed 's/"/\\"/g')"
}
JSON
else
  note "── 環境變數名稱（不印值）"; note "  ${env_names}"
  note "── 掛載（/secrets 與 /work）"; note "${mounts}"
  note "── docker.sock: ${docker_sock}"
  note "── CapEff: ${cap_eff}"
  note "── /Users 可見內容: '${users_visible}'"
  note "── /secrets 是否可寫: ${secrets_writable}"
  note "── 秘密值在 /work 裡的命中數: ${leak_total}（${leak_detail}）"
  note "── Claude 相關環境變數: '${claude_env}'"
  note "── Claude 相關檔案: '${claude_files}'"
fi

# ── 判定 ───────────────────────────────────────────────────────────────
[ "$docker_sock" = "absent" ] || bad "docker.sock 在容器內可達（D3：不可掛）"
[ "$secrets_writable" = "read-only" ] || bad "/secrets 可寫（D3：憑證只能唯讀）"
[ "$leak_total" -eq 0 ] || bad "秘密的值出現在 /work 裡（命中 ${leak_total} 處）"
[ -z "$claude_env" ] || bad "存在 Claude 相關環境變數（PM 決定 2：不用 Claude）"
[ -z "$claude_files" ] || bad "存在 Claude 相關檔案或明文 auth.json"
[ -z "$users_visible" ] || bad "可以看到宿主機的 /Users（${users_visible}）"
[ "$cap_eff" = "0000000000000000" ] || note "（注意）CapEff 非零：${cap_eff}"
[ "${secrets_mount_count:-0}" -ge 1 ] || bad "沒有任何 /secrets 掛載"
[ "${work_mount_count:-0}" -ge 1 ] || bad "沒有 /work 掛載"

[ "$as_json" = 1 ] || { [ "$fail" = 0 ] && ok "能力邊界檢查全部通過" || echo "能力邊界檢查有失敗項（見上）" >&2; }
exit "$fail"
