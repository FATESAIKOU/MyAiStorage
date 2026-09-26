#!/usr/bin/env bash
# 技術驗證用的容器環境建置腳本（Mac / colima）。
# 預設 build 成 colima 的 server 架構（Apple Silicon 是 arm64）；
# 用 ARCH=amd64 可以建 amd64 版（colima 有 qemu/binfmt 才行）——1.2 要跟 Actions runner 比對。
# 只 build image，不建立任何 Drive / GitHub 資源、不需要任何憑證。
set -euo pipefail

IMAGE="${IMAGE:-aistorage-spike-env:latest}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v docker >/dev/null 2>&1 || { echo "找不到 docker" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "docker daemon 沒在跑（colima start）" >&2; exit 1; }

case "${ARCH:-}" in
  "") arch="$(docker version --format '{{.Server.Arch}}')" ;;
  arm64|amd64) arch="${ARCH}" ;;
  *) echo "不支援的 ARCH=${ARCH}（只支援 arm64 / amd64）" >&2; exit 2 ;;
esac

echo "build ${IMAGE}（platform: linux/${arch}）"

exec docker build \
  --platform "linux/${arch}" \
  --build-arg "TARGETARCH=${arch}" \
  --tag "${IMAGE}" \
  "${HERE}"
