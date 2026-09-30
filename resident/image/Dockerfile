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

# BuildKit 會自動帶 TARGETARCH；colima 預設沒有 buildx（legacy builder 不給），
# 所以兩邊都拿得到值：優先用 TARGETARCH，沒有就用 uname -m 判斷。
ARG TARGETARCH

# rclone 1.75.1（arm64/amd64 的 SHA256 取自 spike/env/Dockerfile）
ARG RCLONE_VERSION=1.75.1
ARG RCLONE_SHA256_ARM64=03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9
ARG RCLONE_SHA256_AMD64=982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab
# opencode 1.18.32（同上）
ARG OPENCODE_VERSION=1.18.32
ARG OPENCODE_SHA256_ARM64=568461b7d4d8c19865c97e9a1102e613049c6039d01fe772154de873c1865840
ARG OPENCODE_SHA256_AMD64=3046e0404fdc60fb80307e7a47824ba07477364178a4d09baa8548496dd6d43b
# Python 用 Ubuntu 24.04 內附的 3.12（下方 apt 以 3.12.3* 釘住小版本）。
# 容器執行期不需要 uv：wheel 在宿主機由 `uv build` 產生，容器裡只安裝它。

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PATH="/opt/aistorage/venv/bin:/usr/local/bin:/usr/bin:/bin"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      jq \
      less \
      python3=3.12.3* \
      python3-venv=3.12.3* \
      ripgrep \
      sqlite3 \
      unzip \
      xz-utils \
 && rm -rf /var/lib/apt/lists/*

# ── rclone（同步器與 skill 透過它使用 worker 的 drive.file 憑證）────────────
RUN set -eux; \
    arch="${TARGETARCH:-$(uname -m)}"; \
    case "$arch" in \
      arm64|aarch64) arch=arm64; rclone_sha="${RCLONE_SHA256_ARM64}" ;; \
      amd64|x86_64) arch=amd64; rclone_sha="${RCLONE_SHA256_AMD64}" ;; \
      *) echo "unsupported arch: ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${arch}.zip" -o /tmp/rclone.zip; \
    echo "${rclone_sha}  /tmp/rclone.zip" | sha256sum -c -; \
    unzip -q /tmp/rclone.zip -d /tmp; \
    install -m 0755 "/tmp/rclone-v${RCLONE_VERSION}-linux-${arch}/rclone" /usr/local/bin/rclone; \
    rm -rf /tmp/rclone.zip "/tmp/rclone-v${RCLONE_VERSION}-linux-${arch}"; \
    RCLONE_VERSION= rclone version | head -1

# ── opencode（住民 AI 本體；不寫死模型與 provider）─────────────────────────
RUN set -eux; \
    arch="${TARGETARCH:-$(uname -m)}"; \
    case "$arch" in \
      arm64|aarch64) oc_arch=arm64; oc_sha="${OPENCODE_SHA256_ARM64}" ;; \
      amd64|x86_64)   oc_arch=x64;   oc_sha="${OPENCODE_SHA256_AMD64}" ;; \
      *) echo "unsupported arch: ${arch}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${oc_arch}.tar.gz" -o /tmp/opencode.tar.gz; \
    echo "${oc_sha}  /tmp/opencode.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/opencode.tar.gz -C /tmp opencode; \
    install -m 0755 /tmp/opencode /usr/local/bin/opencode; \
    rm -f /tmp/opencode /tmp/opencode.tar.gz; \
    opencode --version

# JSON Schema 檔（aistorage.schema 執行期會找 schemas/；wheel 裡沒有帶這一層）
COPY schemas/ /opt/aistorage/schemas/

# ── Python 3.12 venv + aistorage 套件 ──────────────────────────────────
ARG PYTHON_APT_VERSION=3.12.3
RUN set -eux; \
    python3 -m venv /opt/aistorage/venv; \
    /opt/aistorage/venv/bin/python -V

# aistorage 套件（由 build.sh 先在宿主機 `uv build` 產生 wheel）。
# Ubuntu 24.04 沒有 python3-pip（PEP 668 也要 system 環境），所以 venv 內用
# ensurepip 取得 pip，不另外裝套件。
COPY dist/*.whl /tmp/wheels/
# cryptography 47+ 的 aarch64 wheel 用了 colima VM 沒暴露的指令
# （`from cryptography.hazmat.primitives import hashes` 會 SIGILL；
#   VM 的 Features 沒有 armv8.2+ 的 LSE/SHA 系列）。46.0.3 在同一個 VM 上正常。
# 這是**容器執行環境**的限制，Mac 上的開發環境不受影響。
ARG CRYPTOGRAPHY_SPEC="cryptography>=42,<47"
RUN set -eux; \
    /opt/aistorage/venv/bin/python -m ensurepip --upgrade; \
    /opt/aistorage/venv/bin/pip install --no-cache-dir /tmp/wheels/aistorage-*.whl; \
    rm -rf /tmp/wheels; \
    /opt/aistorage/venv/bin/pip install --no-cache-dir "${CRYPTOGRAPHY_SPEC}"; \
    cd /opt/aistorage; \
    /opt/aistorage/venv/bin/python -c "import aistorage, aistorage.inbox_builder, aistorage.reader; from cryptography.hazmat.primitives import hashes; print('aistorage import ok')"; \
    ls /opt/aistorage/schemas | head -3; \
    opencode --version; \
    rclone --version | head -1

# 5.4 的 plugin 與 skill 說明（build context 是 repo 根目錄）
COPY resident/opencode/plugin/ /opt/aistorage/opencode/plugin/
COPY resident/opencode/skills/ /opt/aistorage/opencode/skills/
COPY resident/opencode/opencode.base.json /opt/aistorage/opencode/opencode.base.json
COPY resident/image/entrypoint.sh /opt/aistorage/entrypoint.sh
RUN chmod 0755 /opt/aistorage/entrypoint.sh

# /secrets 唯讀掛載。權限是 0711（可穿越、不可列目錄）：容器以 Mac 的 uid 執行，
# 若目錄本身是 root 的 0700，非 root 使用者連 stat 都做不到（會誤判成檔案不存在）。
# 每個檔案本身在宿主機是 0600 且唯讀掛載。
RUN set -eux; \
    mkdir -p /secrets /work; \
    chmod 0711 /secrets; \
    chmod 0777 /work

ENV HOME=/work
WORKDIR /work

# 不設死模型：provider 與模型由 run.sh --model 決定，entrypoint 產生 opencode.json
ENV OPENCODE_CONFIG=/tmp/aistorage/opencode.json
ENV AISTORAGE_RCLONE_CONF=/tmp/aistorage/rclone.conf
ENV AISTORAGE_SECRETS_DIR=/secrets

ENTRYPOINT ["/opt/aistorage/entrypoint.sh"]
CMD ["bash", "-l"]
