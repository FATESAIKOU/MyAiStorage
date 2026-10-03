#!/usr/bin/env bash
# Drive（rclone + drive.file）的驗證腳本（spike V4）。
#
# 邊界（一定要守住）：
# - 只動 gdrive:agora-test/ 底下自己建的資料夾。drive.file 看得到的就是這些。
# - 憑證只用路徑引用（--config "$AGORA_RCLONE_CONF"），**不讀、不印**。
# - 收尾一定刪掉 agora-test/（purge），不留垃圾在 Drive 上。
#
# 用法：drive_probe.sh [remote 根路徑]   預設 gdrive:agora-test
#
# ⚠️ zsh 的話 `$VAR` 不會自動分字，所以 rclone 的參數全部用引號寫死，不要靠
#    變數展開（`rclone $FLAGS lsd gdrive:` 在 zsh 會把整個 "--config ..." 當成
#    一個引數，rclone 直接報 "unknown command"）。
set -uo pipefail

CONF="${AGORA_RCLONE_CONF:-$HOME/.config/agora/rclone.conf}"
REMOTE_ROOT="${1:-gdrive:agora-test}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/agora-drive-probe.XXXXXX")"
MIRROR="$WORK/mirror"
mkdir -p "$MIRROR"

# 每個 rclone 呼叫都可能慢到需要人等：統一給 timeout，否則 spike 會卡住。
r() { timeout 90 rclone --config "$CONF" "$@"; rc=$?; echo "  [rc=$rc] rclone $*"; return $rc; }

say() { printf '\n=== %s ===\n' "$*"; }

ulid() {
  python3 - <<'PY'
import secrets, time
alpha = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"   # Crockford base32
t = int(time.time() * 1000)
stamp = "".join(alpha[(t >> (5 * (9 - i))) & 31] for i in range(10))
print(stamp + "".join(secrets.choice(alpha) for _ in range(14)))
PY
}

say "0. 前置：remote 存在、看得見自己的檔案"
r listremotes
r lsd "gdrive:"

say "1. 建 sessions/<ULID>/"
ULID="$(ulid)"
SESSION_DIR="$REMOTE_ROOT/sessions/$ULID"
echo "  ULID=$ULID"
r mkdir "$SESSION_DIR"

say "2. 上傳：copy 給「檔名當目的地」會建成資料夾，copyto 才會上傳成檔案"
printf '%s\n' "---" "entity: agora" "type: session" "id: agora:01K6SPIKEDRIVEV4AA" "---" "" "# spike 測試 session" > "$WORK/session.md"
printf '{"info":{"id":"ses_placeholder"},"messages":[]}\n' > "$WORK/raw.json"
r copy "$WORK/session.md" "$SESSION_DIR/trap.md"       # 陷阱：目的地不是以 / 結尾
r lsf "$SESSION_DIR" --format "p" --dirs-only
r copyto "$WORK/session.md" "$SESSION_DIR/session.md"   # 正確：單檔對單檔
r copyto "$WORK/raw.json" "$SESSION_DIR/raw.json"
r lsf "$SESSION_DIR" --format "pst"
r lsjson "$SESSION_DIR"

say "3. 同名資料夾 / 同名資料夾 vs 檔案"
r mkdir "$REMOTE_ROOT/sessions/dup"
r mkdir "$REMOTE_ROOT/sessions/dup"                        # 第二次：冪等還是報錯？
r lsf "$REMOTE_ROOT/sessions" --format "p" --dirs-only
r copyto "$WORK/raw.json" "$REMOTE_ROOT/sessions/dup"      # 檔名撞到既有資料夾
r lsf "$REMOTE_ROOT/sessions" --format "pst"

say "4. 用 folder ID 當根（--drive-root-folder-id，目標寫 gdrive:）"
FOLDER_ID="$(timeout 90 rclone --config "$CONF" lsjson "$SESSION_DIR" --dirs-only 2>/dev/null | jq -r '.[] | select(.Name=="trap.md") | .ID' | head -1)"
echo "  folder_id_len=${#FOLDER_ID}"
timeout 90 rclone --config "$CONF" --drive-root-folder-id "$FOLDER_ID" lsf "gdrive:" --format "pst"
echo "  讀檔（cat）："
timeout 90 rclone --config "$CONF" --drive-root-folder-id "$FOLDER_ID" cat "gdrive:session.md" | head -3
echo "  反例：目標寫 '.' 會列本機目錄，不是 remote："
timeout 90 rclone --config "$CONF" --drive-root-folder-id "$FOLDER_ID" lsf . --format "p" | head -3

say "5. 增量下載：第一次全拿"
r copy "$SESSION_DIR" "$MIRROR" --include "session.md" --include "raw.json"
ls -l "$MIRROR"

say "5b. 第二次（遠端沒變）應該什麼都不傳"
r copy "$SESSION_DIR" "$MIRROR" --include "session.md" --include "raw.json" -v 2>&1 | grep -E "nothing to transfer|Transferred|Checks" || true

say "5c. 遠端新增一個檔案，第二次只補那個"
printf 'later\n' > "$WORK/raw2.json"
r copyto "$WORK/raw2.json" "$SESSION_DIR/raw2.json"
r copy "$SESSION_DIR" "$MIRROR" --include "raw2.json"
ls -l "$MIRROR"

say "5d. 覆寫已有的 session.md（同一個 session 重新匯入）"
printf '%s\n' "---" "entity: agora" "type: session" "id: agora:01K6SPIKEDRIVEV4AA" "---" "" "# 更新過的閱讀版" > "$WORK/session-v2.md"
r copyto "$WORK/session-v2.md" "$SESSION_DIR/session.md"
r copy "$SESSION_DIR" "$MIRROR" --include "session.md"
rtk read "$MIRROR/session.md"

say "6. Drive 端刪掉一個檔案後，鏡像要不要自己清（同步策略）"
r deletefile "$SESSION_DIR/raw2.json"
ls -l "$MIRROR"

say "7. token 到期（只印 expiry，不印 token 本身）"
timeout 60 rclone --config "$CONF" config dump 2>/dev/null | jq -r '.gdrive | {type, scope, team_drive, token:(.token|fromjson|{token_type, expiry})}'
date -u +"now=%Y-%m-%dT%H:%M:%SZ"

say "8. 收尾：purge 掉整個測試資料夾"
r purge "$REMOTE_ROOT"
r lsd "gdrive:"
echo "done（work dir: ${WORK}）"