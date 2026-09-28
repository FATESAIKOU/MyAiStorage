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
# 掛載點是 /work 與 /secrets/<檔名>（每個白名單檔案各一個 bind mount）
mounts="$(awk '$2 == "/work" || $2 ~ "^/secrets/"' /proc/mounts 2>/dev/null || true)"
secrets_mount_count="$(printf '%s\n' "$mounts" | awk '$2 ~ "^/secrets/"' | wc -l | tr -d ' ')"
work_mount_count="$(printf '%s\n' "$mounts" | awk '$2 == "/work"' | wc -l | tr -d ' ')"
# /proc/mounts 的欄位是 source target fstype options（選項在 $4，不是 $3）
secrets_mounts_rw="$(printf '%s\n' "$mounts" | awk '$2 ~ "^/secrets/" && $4 !~ /(^|,)ro(,|$)/' | wc -l | tr -d ' ')"
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
# 比對前一定要先把**太短的行**濾掉：`grep -F -f` 會把 pattern 檔裡的每一行
# 都當成一個字串比對，而 JSON／conf 裡有 `{`、`}`、`},` 這種 1～2 字元的行。
# 實測：sa-reader.json 有一行是單字元，於是 /work 底下 opencode 裝的
# node_modules 裡幾千個 JSON 檔全部「命中」，整個檢查變成沒有訊號。
# 秘密的實質內容（token／私鑰／client_email…）都遠超過這個門檻。
#
# **不能用 `${SECRETS_DIR}/*` 列舉**：`/secrets` 是 0711（可穿越、不可列目錄），
# glob 不會展開，迴圈整個不跑，`secret_leak_hits` 會永遠是 0（假的通過）。
# 所以照白名單一個一個指名。
#
# `reader.json` **不掃**：它是刻意放在 /secrets 的非秘密設定（manifest id、
# inbox folder id），裡面都是 `  "inbox_folder_ids": {` 這種通用 JSON 鍵，
# 拿它去比對 /work 底下任何一個 JSON 都會命中（實測會撞到 schemas/*.json），
# 只會製造假警報。真正的秘密是 rclone-worker.conf／sa-reader.json／
# signing.key／llm-<provider>.key／gh-pat-actions.txt。
MIN_PATTERN_LEN=12
SECRET_NAMES="rclone-worker.conf sa-reader.json signing.key gh-pat-actions.txt"
# LLM 金鑰的檔名帶 provider，run.sh 只掛 `--model` 指定的那一個。沒有 glob
# 可用，所以探幾個期 1 可能的 provider（不存在的檔案會被 [ -f ] 跳過），
# 也可以用 AISTORAGE_LLM_KEY_NAME 指定實際的名稱。
SECRET_NAMES="$SECRET_NAMES ${AISTORAGE_LLM_KEY_NAME:-llm-opencode.key llm-ollama.key llm-openrouter.key}"
leak_total=0
leak_detail=""
leak_scanned=0
for name in $SECRET_NAMES; do
  f="${SECRETS_DIR}/${name}"
  [ -f "$f" ] || continue
  leak_scanned=$((leak_scanned + 1))
  # 夠長的行才是 pattern；短行只是格式
  patterns="$(mktemp)"
  awk -v n="$MIN_PATTERN_LEN" 'length($0) >= n' "$f" > "$patterns"
  if [ ! -s "$patterns" ]; then
    # 全部都是短行（例如只有一行 token 但檔案被換行切爛）：整個檔案當一個 pattern
    cp "$f" "$patterns"
  fi
  # 計數的是「有多少個檔案命中」，不是命中行數（輸出只放計數，不放內容）
  count="$(grep -r -c -F -f "$patterns" "$WORK_DIR" 2>/dev/null \
    | awk -F: '$NF>0{n++} END{print n+0}')"
  rm -f "$patterns"
  leak_total=$((leak_total + count))
  leak_detail="${leak_detail}${name}=${count} "
done
note "秘密比對掃過的檔案數：${leak_scanned}"
# ── 8. Claude 憑證 ─────────────────────────────────────────────────────
claude_env="$(env | sed 's/=.*//' | grep -E '^(ANTHROPIC|CLAUDE)' | tr '\n' ' ')"
claude_files=""
for p in /work/.claude.json /work/.claude /work/.config/anthropic \
         /work/.config/opencode/auth.json /work/.local/share/opencode/auth.json; do
  [ -e "$p" ] && claude_files="${claude_files}${p} "
done

# 一個秘密檔都沒掃到＝等於沒檢查（/secrets 不可列目錄時 glob 不會展開）
if [ "$leak_scanned" -eq 0 ]; then
  fail=1
  leak_detail="沒有掃到任何秘密檔（檢查無效） "
fi

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
  "secrets_mounts_not_readonly": ${secrets_mounts_rw:-0},
  "secret_leak_hits": ${leak_total},
  "secret_leak_scanned": ${leak_scanned},
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
[ "${secrets_mounts_rw:-1}" = "0" ] || bad "有 ${secrets_mounts_rw} 個 /secrets 掛載不是唯讀（rw）"
[ "$leak_total" -eq 0 ] || bad "秘密的值出現在 /work 裡（命中 ${leak_total} 處）"
[ -z "$claude_env" ] || bad "存在 Claude 相關環境變數（PM 決定 2：不用 Claude）"
[ -z "$claude_files" ] || bad "存在 Claude 相關檔案或明文 auth.json"
[ -z "$users_visible" ] || bad "可以看到宿主機的 /Users（${users_visible}）"
[ "$cap_eff" = "0000000000000000" ] || note "（注意）CapEff 非零：${cap_eff}"
[ "${secrets_mount_count:-0}" -ge 1 ] || bad "沒有任何 /secrets 掛載"
[ "${work_mount_count:-0}" -ge 1 ] || bad "沒有 /work 掛載"

[ "$as_json" = 1 ] || { [ "$fail" = 0 ] && ok "能力邊界檢查全部通過" || echo "能力邊界檢查有失敗項（見上）" >&2; }
exit "$fail"
