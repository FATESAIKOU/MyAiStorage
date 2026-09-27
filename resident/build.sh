#!/usr/bin/env bash
# 建立住民容器的 image（tasks 5.1）。沿用 spike/env/build.sh 的做法。
#
# 用法：resident/build.sh [--no-cache]
#
# 步驟：
#   1. 在 repo 根目錄 `uv build` 產生 wheel（Dockerfile 會 COPY 進去安裝）
#   2. docker build，context 是 repo 根目錄（需要 dist/ 與 resident/）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/.." && pwd)"
IMAGE="${AISTORAGE_RESIDENT_IMAGE:-aistorage-resident:latest}"

no_cache=0
[ "${1:-}" = "--no-cache" ] && no_cache=1

command -v docker >/dev/null 2>&1 || { echo "找不到 docker" >&2; exit 1; }
command -v uv >/dev/null 2>&1 || { echo "找不到 uv（https://docs.astral.sh/uv/）" >&2; exit 1; }

echo "[build] 產生 wheel：${REPO_ROOT}/dist"
( cd "$REPO_ROOT" && uv build --wheel --out-dir dist )

# 5.4 的 plugin／skill 說明先給一個最小的骨架，之後由 5.4 覆寫。
mkdir -p "${HERE}/opencode/plugin" "${HERE}/opencode/skills"
[ -f "${HERE}/opencode/plugin/aistorage.ts" ] || \
  printf '// 5.4 會放這裡；先留空檔讓 image 內的路徑存在。\nexport default async () => ({})\n' \
    > "${HERE}/opencode/plugin/aistorage.ts"

cache_arg=()
[ "$no_cache" = 1 ] && cache_arg=(--no-cache)

# 架構：colima 預設沒有 buildx（legacy builder 不帶 TARGETARCH），明確傳進去；
# Dockerfile 內部也會用 uname -m 兜底，所以兩種 builder 都能建。
# --platform 也要給：本機的 ubuntu:24.04 標籤曾快取成 amd64，會讓 arm64 的
# opencode 靜態執行檔在容器裡「required file not found」。
case "$(uname -m)" in
  arm64) target_arch=arm64; platform=linux/arm64 ;;
  x86_64) target_arch=amd64; platform=linux/amd64 ;;
  *) echo "不支援的架構：$(uname -m)" >&2; exit 1 ;;
esac

echo "[build] docker build：${IMAGE}"
exec docker build ${cache_arg[@]+"${cache_arg[@]}"} \
  --build-arg "TARGETARCH=${target_arch}" \
  --platform "$platform" \
  --file "${HERE}/image/Dockerfile" \
  --tag "$IMAGE" \
  "$REPO_ROOT"
