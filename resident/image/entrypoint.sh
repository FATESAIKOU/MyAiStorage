#!/usr/bin/env bash
# 住民容器的啟動流程（tasks 5.1；group5-7 第 1.2 節）。
#
# 依序做：
#   0. 自我檢查：沒有 Claude 的憑證、沒有明文 auth.json
#   1. rclone-worker.conf → /tmp/aistorage/rclone.conf（600，可寫，讓 token 能刷新）
#   2. 由 opencode.base.json 產生 /tmp/aistorage/opencode.json（provider 金鑰是
#      檔案引用，不是明文），設定 OPENCODE_CONFIG
#   3. 背景啟動 opencode serve（同步器的 API 用它）
#   4. 背景啟動同步器 daemon
#   5. 前景執行使用者的介面（預設 TUI attach 到既有的 serve；PM 決定 1：
#      opencode 1.18.32 有 `opencode attach <url>`）
#
# 秘密只以路徑引用：這個腳本不讀、不印任何秘密的內容。
set -euo pipefail

OPENCODE_BIN="${OPENCODE_BIN:-opencode}"
SECRETS_DIR="${AISTORAGE_SECRETS_DIR:-/secrets}"
STATE_DIR="${AISTORAGE_STATE_DIR:-/tmp/aistorage}"
OPENCODE_TEMPLATE="${AISTORAGE_OPENCODE_TEMPLATE:-/opt/aistorage/opencode/opencode.base.json}"
SERVE_PORT="${AISTORAGE_OPENCODE_PORT:-4096}"
SERVE_HOST="${AISTORAGE_OPENCODE_HOST:-127.0.0.1}"
SYNCER_INTERVAL="${AISTORAGE_SYNC_INTERVAL:-600}"
RCLONE_CONF="${AISTORAGE_RCLONE_CONF:-${STATE_DIR}/rclone.conf}"
OPENCODE_CONFIG="${OPENCODE_CONFIG:-${STATE_DIR}/opencode.json}"

die() { echo "[entrypoint] $*" >&2; exit 1; }

# 只列檔名，不印內容。
require_file() {
  local name="$1" why="$2"
  [ -f "${SECRETS_DIR}/${name}" ] || die "缺少必要檔案：/secrets/${name}（${why}）"
}

# 容器內不該出現的東西：Claude 憑證、明文 auth.json。
refuse_claude() {
  local var
  for var in ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN CLAUDE_API_KEY \
             CLAUDE_CODE_OAUTH_TOKEN ANTHROPIC_BASE_URL; do
    if [ -n "${!var-}" ]; then
      die "拒絕啟動：環境變數 ${var} 存在。住民容器不使用 Claude（PM 決定 2）"
    fi
  done
  # 全部用 ${HOME}（容器內 HOME=/work，但測試會把 HOME 指到別處；
  # 寫死 /work 會讓 review-g5-6 L1 的測試誤判）
  local p
  for p in \
      "${HOME}/.claude.json" \
      "${HOME}/.config/anthropic" \
      "${HOME}/.claude/settings.json" \
      "${HOME}/.claude/.credentials.json" \
      "${HOME}/.config/opencode/auth.json" \
      "${HOME}/.local/share/opencode/auth.json" ; do
    if [ -e "$p" ]; then
      die "拒絕啟動：${p} 存在。住民容器的憑證一律以 /secrets 的檔案引用提供，opencode 不該寫出明文 auth.json（1.7a M4）；請刪除該檔再啟動"
    fi
  done
}

# ── 免金鑰的 provider ──────────────────────────────────────────────────
# opencode zen 的免費模型（opencode/space-bunny-free 等）匿名可用、依 IP 限流，
# 不需要 apiKey。實測（2026-09-28，容器內、沒有 auth.json、沒有任何金鑰檔）：
# `回覆 OK` 4 秒回完、finish=stop、cost=0。
# 這裡只是「不要求金鑰檔」；白名單規則不變（你還是可以放 llm-<provider>.key，
# 有的話照樣掛載並使用）。
KEYLESS_PROVIDERS="${AISTORAGE_KEYLESS_PROVIDERS:-opencode}"

provider_needs_no_key() {
  local provider="$1" p
  for p in $KEYLESS_PROVIDERS; do
    [ "$p" = "$provider" ] && return 0
  done
  return 1
}

# ── 產生 opencode.json（唯讀輸出模式可單獨測試）──────────────────────────
# 用法：entrypoint.sh render-config <provider> <model> [base.json]
# require_key=0 時不檢查金鑰檔是否存在（render-config 模式：只驗證範本與替換結果）
render_config() {
  local provider="$1" model="$2" template="${3:-$OPENCODE_TEMPLATE}" require_key="${4:-1}"
  # 用 ${SECRETS_DIR}（容器內就是 /secrets）而不是寫死 /secrets：
  # 這樣在主機上測 render-config 時也能真的驗到「有金鑰／沒金鑰」兩條路。
  local key_file="${SECRETS_DIR}/llm-${provider}.key"
  [ -n "$provider" ] || die "render-config 需要 provider"
  [ -n "$model" ] || die "render-config 需要 model（格式 <provider>/<model>）"
  case "$model" in
    */*) : ;;
    *) die "model 必須是 <provider>/<model>：${model}" ;;
  esac
  case "$model" in
    "${provider}"/*) : ;;
    *) die "model 的 provider（${model%%/*}）與指定的 provider（${provider}）不一致" ;;
  esac
  [ -f "$template" ] || die "找不到 opencode 設定範本：${template}"
  # 沒有金鑰檔就是「這個 provider 匿名可用」（例如 opencode zen 的免費模型：
  # 匿名、依 IP 限流）。這種情況**整個移除 provider 設定**，不要留一個指向
  # 不存在檔案的 {file:} 參照——實測那樣連 POST /session 都會 400。
  # 有金鑰 → 檔案引用；沒有金鑰 → 要嘛是免金鑰的 provider（移除 provider 區塊），
  # 要嘛直接拒絕啟動。`render-config`（require_key=0）是**唯讀渲染**模式：主機上
  # 沒有 /secrets 很正常，所以那個模式不做這個判斷、也不移除 provider 區塊。
  # AISTORAGE_FORCE_NO_KEY=1 可以在渲染模式下模擬「沒有金鑰檔」，給測試用。
  have_key=0
  if [ -f "$key_file" ]; then
    have_key=1
  elif [ "${AISTORAGE_FORCE_NO_KEY:-0}" = "1" ]; then
    have_key=0
    echo "[entrypoint] AISTORAGE_FORCE_NO_KEY=1：模擬沒有金鑰檔" >&2
  elif [ "$require_key" = "1" ]; then
    if provider_needs_no_key "$provider"; then
      echo "[entrypoint] provider=${provider} 免金鑰（匿名可用），產生的設定不會帶 apiKey" >&2
    else
      die "缺少 LLM 金鑰：${key_file}（依 --model 的 provider 選擇）"
    fi
  else
    have_key=1   # 唯讀渲染：維持範本原樣（不因為主機上沒有檔案就改設定）
  fi

  local out
  out="$(mktemp)"
  # 只替換三個 placeholder：金鑰**永遠**是檔案引用，絕不把內容讀進設定檔。
  sed -e "s|__MODEL__|${model}|g" \
      -e "s|__PROVIDER__|${provider}|g" \
      -e "s|__API_KEY_FILE__|{file:${key_file}}|g" \
      "$template" > "$out"
  if [ "$have_key" = "0" ]; then
    local trimmed
    trimmed="$(mktemp)"
    if ! jq --arg p "$provider" 'if .provider[$p] then .provider |= del(.[$p]) else . end' \
         "$out" > "$trimmed"; then
      rm -f "$out" "$trimmed"; die "移除免金鑰 provider 的設定失敗（範本：${template}）"
    fi
    mv "$trimmed" "$out"
  fi
  if ! jq -e . "$out" >/dev/null; then
    rm -f "$out"; die "產生的 opencode.json 不是合法 JSON（範本：${template}）"
  fi
  # 自我檢查：產物裡不得出現金鑰檔以外的秘密，也不得出現明文 key 欄位。
  if jq -e '.provider | to_entries[] | select(.value.options.apiKey? != null)
        | select(.value.options.apiKey | startswith("{file:") | not)' "$out" >/dev/null; then
    rm -f "$out"; die "產生的設定含有非檔案引用的 apiKey（必須是 {file:/secrets/...}）"
  fi
  cat "$out"
  rm -f "$out"
}

# 模式：serve（預設，完整流程）／render-config（唯讀渲染設定）／其他 → 直接執行該指令
# （run.sh 傳進來的指令，例如 `bash -c 'opencode --version'` 這種除錯用的一次性命令；
#  這條路徑刻意不做前置檢查與背景服務，只給除錯與能力邊界錄影用）。
mode="${1:-serve}"
case "$mode" in
  render-config)
    shift
    # 第 3 個參數是「範本路徑」，沒有給就留空讓 render_config 用預設。
    # （直接 `render_config "$@" 0` 會把 require_key 的 0 塞到範本的位置。）
    case "$#" in
      3) render_config "$1" "$2" "$3" 0 ;;
      2) render_config "$1" "$2" "" 0 ;;
      *) usage ;;
    esac
    exit 0
    ;;
  serve) ;;
  # 除錯路徑（`bash -c '...'` 這種一次性命令）本來會整個略過前置檢查。
  # review-g5-6 L1：連這種路徑也要先擋 Claude 的憑證——否則「拒絕 Claude」
  # 就變成只要走這條路徑就繞得過去。
  -*) refuse_claude; exec "$@" ;;
  *) refuse_claude; exec "$@" ;;
esac

model="${AISTORAGE_MODEL:-}"
provider="${AISTORAGE_PROVIDER:-${model%%/*}}"
[ -n "$model" ] || die "缺少模型設定：AISTORAGE_MODEL（由 run.sh --model 傳入，格式 <provider>/<model>）"

refuse_claude
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

# aistorage.schema 執行期會找 schemas/（wheel 裡沒有帶）。容器的 CWD 是 /work，
# 所以把 image 內 /opt/aistorage/schemas 的 json 複製一份到 /work/schemas；
# 已存在就不覆寫（使用者若自行放了新的 schema，保留 theirs）。
SCHEMA_SRC="${AISTORAGE_SCHEMA_SRC:-/opt/aistorage/schemas}"
if [ -d "$SCHEMA_SRC" ]; then
  mkdir -p "${WORK:-/work}/schemas"
  for f in "$SCHEMA_SRC"/*.json; do
    [ -e "$f" ] || continue
    dest="${WORK:-/work}/schemas/$(basename "$f")"
    [ -e "$dest" ] || cp "$f" "$dest"
  done
fi

# skill 說明走 `opencode.base.json` 的 `skills.paths`（唯讀的
# /opt/aistorage/opencode/skills），不在這裡複製到 /work——避免有兩份會漂移的說明。

# ── 1. rclone-worker.conf → 可寫的副本（token 要能刷新）───────────────────
require_file rclone-worker.conf "同步器上傳收件匣用（worker 的 drive.file 憑證）"
install -m 0600 "${SECRETS_DIR}/rclone-worker.conf" "$RCLONE_CONF"

# ── 2. opencode.json（金鑰是檔案引用）────────────────────────────────────
render_config "$provider" "$model" "$OPENCODE_TEMPLATE" > "${OPENCODE_CONFIG}.tmp"
mv "${OPENCODE_CONFIG}.tmp" "$OPENCODE_CONFIG"
chmod 0600 "$OPENCODE_CONFIG"

# 其餘白名單檔案：sync-and-commit 與讀取用
if [ -f "${SECRETS_DIR}/signing.key" ]; then
  export AISTORAGE_SIGNING_KEY="${SECRETS_DIR}/signing.key"
fi
if [ -f "${SECRETS_DIR}/reader.json" ]; then
  export AISTORAGE_READER_CONFIG="${SECRETS_DIR}/reader.json"
fi
if [ -f "${SECRETS_DIR}/sa-reader.json" ]; then
  export AISTORAGE_SA_KEY="${SECRETS_DIR}/sa-reader.json"
fi
export AISTORAGE_PROFILE="${AISTORAGE_PROFILE:-mac-opencode}"

# ── 3. opencode serve（背景）────────────────────────────────────────────
"${OPENCODE_BIN}" serve --hostname "$SERVE_HOST" --port "$SERVE_PORT" \
  >"${STATE_DIR}/opencode-serve.log" 2>&1 &
serve_pid=$!
ready=0
for _ in $(seq 1 60); do
  # 一定要有 --max-time：serve 剛起來時第一次 /session 可能連得上卻不回應，
  # 沒有逾時的 curl 會把整個 entrypoint 卡住（實測過）。
  if curl -fsS --connect-timeout 2 --max-time 5 \
       "http://${SERVE_HOST}:${SERVE_PORT}/session" >/dev/null 2>&1; then ready=1; break; fi
  kill -0 "$serve_pid" 2>/dev/null || die "opencode serve 起動失敗（看 ${STATE_DIR}/opencode-serve.log）"
  sleep 0.5
done
[ "$ready" = 1 ] || die "opencode serve 60 秒內沒有就緒（看 ${STATE_DIR}/opencode-serve.log）"
echo "[entrypoint] opencode serve 已在 http://${SERVE_HOST}:${SERVE_PORT} 服務（pid ${serve_pid}）"

# ── 4. 同步器 daemon（背景；只會在需要時自己同步並提交）────────────────
python -m aistorage.syncer opencode daemon --interval "$SYNCER_INTERVAL" \
  >"${STATE_DIR}/syncer.log" 2>&1 &
syncer_pid=$!
sleep 1
if ! kill -0 "$syncer_pid" 2>/dev/null; then
  echo "[entrypoint] 警告：同步器沒有跑起來（${STATE_DIR}/syncer.log）；容器繼續提供 serve" >&2
  syncer_pid=""
fi

cleanup() {
  [ -n "$syncer_pid" ] && kill "$syncer_pid" 2>/dev/null || true
  kill "$serve_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# ── 5. 前景：使用者的介面 ───────────────────────────────────────────────
# PM 決定 1：opencode 1.18.32 支援 `opencode attach <url>`，所以容器內的 TUI
# 直接附在既有的 serve 上（同一次對話、同一份 session 狀態）。
# AISTORAGE_RESIDENT_NO_TUI=1 時只跑 serve（測試與 CI 用）。
if [ "${AISTORAGE_RESIDENT_NO_TUI:-0}" = "1" ]; then
  echo "[entrypoint] AISTORAGE_RESIDENT_NO_TUI=1：只啟動 serve 與同步器，前景不做互動"
  wait "$serve_pid"
else
  exec "${OPENCODE_BIN}" attach "http://${SERVE_HOST}:${SERVE_PORT}"
fi
