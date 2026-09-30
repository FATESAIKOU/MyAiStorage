#!/bin/sh
# 健康檢查的 launchd 安裝腳本（PM 決定 8）。
#
# 作用：在 Mac 上每 6 小時執行一次 `python -m aistorage.admin health`。
# 警告：沒有使用者同意不得執行這個腳本。本檔案只放在 repo 備用；
# 單元測試只驗證產生的 plist 內容，從不執行安裝。
#
# 用法（經使用者同意後，由使用者本人執行）：
#   sh scripts/install-health-launchd.sh
# 停用：
#   launchctl bootout gui/$UID/local.aistorage.health
#   rm ~/Library/LaunchAgents/local.aistorage.health.plist
set -eu

LABEL="local.aistorage.health"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
uv run python -m aistorage.admin health --plist > "$PLIST"
chmod 644 "$PLIST"
launchctl bootstrap "gui/$UID" "$PLIST"
echo "installed: $PLIST (interval 6h, notify on fail via exit code)"
