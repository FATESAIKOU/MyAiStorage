# AiStorage 住民容器（Mac 上的 opencode，tasks 5.1）
#
# 設計要點（D3、ADR 0006、group5-7 第 1 節）：
# - 住民 AI 只拿到「白名單裡的憑證」，而且**只能讀**（唯讀掛載到 /secrets/）。
# - 工作目錄每個容器一份（掛到 /work，HOME=/work），容器彼此看不見。
# - 不掛 docker.sock、不加 capability、不以 root 執行（用 Mac 的 uid:gid）。
# - 容器裡**沒有也不該有** Claude 的任何憑證（PM 決定 2：模型是設定值，不用 Claude）。
# - LLM 金鑰以「檔案引用」交給 opencode（{file:/secrets/llm-<provider>.key}），
#   讓它不會在 /work 寫出明文的 auth.json（技術驗證 1.7a M4）。
#
# 版本全部固定並驗 sha256（沿用 spike/env/Dockerfile 的做法）。

FROM ubuntu:24.04

ARG TARGETARCH

# rclone 1.75.1（arm64/amd64 的 SHA256 取自 spike/env/Dockerfile）
ARG RCLONE_VERSION=1.75.1
ARG RCLONE_SHA256_ARM64=03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9
ARG RCLONE_SHA256_AMD64=982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab
# opencode 1.18.32（同上）
ARG OPENCODE_VERSION=1.18.32
ARG OPENCODE_SHA256_ARM64=568461b7d4d8c19865c97e9a1102e613049c6039d01fe772154de873c1865840
ARG OPENCODE_SHA256_AMD64=3046e0404fdc60fb80307e7a47824ba07477364178a4d09baa8548496dd6d43b
# uv 用官方 image（tag 固定，內容由 registry 的 digest 固定），不手寫 sha256。
# Python 3.12 的小版本（由 uv 安裝）
ARG PYTHON_VERSION=3.12.11

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV UV_NO_CACHE=1
ENV UV_PYTHON_INSTALL_DIR=/opt/uv/python
ENV UV_TOOL_DIR=/opt/uv/tools
ENV PATH="/opt/aistorage/venv/bin:/opt/uv/tools:/usr/local/bin:/usr/bin:/bin"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      jq \
      less \
      ripgrep \
      sqlite3 \
      unzip \
      xz-utils \
 && rm -rf /var/lib/apt/lists/*

# ── rclone（同步器與 skill 透過它使用 worker 的 drive.file 憑證）────────────
RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) rclone_sha="${RCLONE_SHA256_ARM64}" ;; \
      amd64) rclone_sha="${RCLONE_SHA256_AMD64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip" -o /tmp/rclone.zip; \
    echo "${rclone_sha}  /tmp/rclone.zip" | sha256sum -c -; \
    unzip -q /tmp/rclone.zip -d /tmp; \
    install -m 0755 "/tmp/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}/rclone" /usr/local/bin/rclone; \
    rm -rf /tmp/rclone.zip "/tmp/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}"

# ── opencode（住民 AI 本體；不寫死模型與 provider）─────────────────────────
RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) oc_arch=arm64; oc_sha="${OPENCODE_SHA256_ARM64}" ;; \
      amd64) oc_arch=x64;   oc_sha="${OPENCODE_SHA256_AMD64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${oc_arch}.tar.gz" -o /tmp/opencode.tar.gz; \
    echo "${oc_sha}  /tmp/opencode.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/opencode.tar.gz -C /tmp opencode; \
    install -m 0755 /tmp/opencode /usr/local/bin/opencode; \
    rm -f /tmp/opencode /tmp/opencode.tar.gz; \
    opencode --version

# ── uv + Python 3.12 + aistorage 套件 ────────────────────────────────────
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
RUN set -eux; \
    uv python install "${PYTHON_VERSION}"; \
    uv venv --python "${PYTHON_VERSION}" /opt/aistorage/venv; \
    /opt/aistorage/venv/bin/python -V

# aistorage 套件（由 build.sh 先在宿主機 `uv build` 產生 wheel）
COPY dist/*.whl /tmp/wheels/
RUN set -eux; \
    uv pip install --python /opt/aistorage/venv/bin/python /tmp/wheels/aistorage-*.whl; \
    rm -rf /tmp/wheels; \
    /opt/aistorage/venv/bin/python -c "import aistorage, aistorage.syncer, aistorage.skill; print(aistorage.__name__)"

# 5.4 的 plugin 與 skill 說明（build context 是 repo 根目錄）
COPY resident/opencode/plugin/ /opt/aistorage/opencode/plugin/
COPY resident/opencode/skills/ /opt/aistorage/opencode/skills/
COPY resident/opencode/opencode.base.json /opt/aistorage/opencode/opencode.base.json
COPY resident/image/entrypoint.sh /opt/aistorage/entrypoint.sh
RUN chmod 0755 /opt/aistorage/entrypoint.sh

# /secrets 唯讀掛載；權限先收緊，掛上後仍不可寫
RUN set -eux; \
    mkdir -p /secrets /work; \
    chmod 0700 /secrets; \
    chmod 0777 /work

ENV HOME=/work
WORKDIR /work

# 不設死模型：provider 與模型由 run.sh --model 決定，entrypoint 產生 opencode.json
ENV OPENCODE_CONFIG=/tmp/aistorage/opencode.json
ENV AISTORAGE_RCLONE_CONF=/tmp/aistorage/rclone.conf
ENV AISTORAGE_SECRETS_DIR=/secrets

ENTRYPOINT ["/opt/aistorage/entrypoint.sh"]
CMD ["bash", "-l"]
