FROM ubuntu:24.04

ARG TARGETARCH

# git-annex 上游只提供 current/ 目錄，沒有版本化 URL；以下 SHA256 對應的版本是
# 10.20260717（arm64/amd64），要換版本得連 SHA256 一起換。
ARG GIT_ANNEX_SHA256_ARM64=d1e78a23ce97c12b1b953466094c1cbb7756220e958d2f2f4dbea20d996b1e08
ARG GIT_ANNEX_SHA256_AMD64=a3c9d10ee33a7ace69efeaa8cd6282cfd7444ec797d732cebae12a64efefa18a
ARG RCLONE_VERSION=1.75.1
ARG RCLONE_SHA256_ARM64=03f2504174034b6d004152ed7369251c9a9ec1f7e0836eda420f5c7a5ec0dff9
ARG RCLONE_SHA256_AMD64=982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab
ARG OPENCODE_VERSION=1.18.32
ARG OPENCODE_SHA256_ARM64=568461b7d4d8c19865c97e9a1102e613049c6039d01fe772154de873c1865840
ARG OPENCODE_SHA256_AMD64=3046e0404fdc60fb80307e7a47824ba07477364178a4d09baa8548496dd6d43b
# Ubuntu 24.04 的 apt 沒有 git-filter-repo；用上游單檔（v2.47.0 的 tag，內容為 python3 腳本）
ARG GIT_FILTER_REPO_SHA256=67447413e273fc76809289111748870b6f6072f08b17efe94863a92d810b7d94

ENV DEBIAN_FRONTEND=noninteractive
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      jq \
      python3 \
      sqlite3 \
      unzip \
      xz-utils \
 && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
      arm64) annex_sha="${GIT_ANNEX_SHA256_ARM64}"; rclone_sha="${RCLONE_SHA256_ARM64}"; oc_arch=arm64; oc_sha="${OPENCODE_SHA256_ARM64}" ;; \
      amd64) annex_sha="${GIT_ANNEX_SHA256_AMD64}"; rclone_sha="${RCLONE_SHA256_AMD64}"; oc_arch=x64; oc_sha="${OPENCODE_SHA256_AMD64}" ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://downloads.kitenet.net/git-annex/linux/current/git-annex-standalone-${TARGETARCH}.tar.gz" -o /tmp/git-annex.tar.gz; \
    echo "${annex_sha}  /tmp/git-annex.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/git-annex.tar.gz -C /opt; \
    rm /tmp/git-annex.tar.gz; \
    ln -s /opt/git-annex.linux/git-annex /usr/local/bin/git-annex; \
    ln -s /opt/git-annex.linux/git-annex-shell /usr/local/bin/git-annex-shell; \
    ln -s /opt/git-annex.linux/git-remote-annex /usr/local/bin/git-remote-annex; \
    sed -i 's/^GIT_ANNEX_PACKAGE_INSTALL=$/GIT_ANNEX_PACKAGE_INSTALL=1/' /opt/git-annex.linux/runshell; \
    grep -qx 'GIT_ANNEX_PACKAGE_INSTALL=1' /opt/git-annex.linux/runshell; \
    curl -fsSL "https://downloads.rclone.org/v${RCLONE_VERSION}/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}.zip" -o /tmp/rclone.zip; \
    echo "${rclone_sha}  /tmp/rclone.zip" | sha256sum -c -; \
    unzip -q /tmp/rclone.zip -d /tmp; \
    install -m 0755 "/tmp/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}/rclone" /usr/local/bin/rclone; \
    rm -rf /tmp/rclone.zip "/tmp/rclone-v${RCLONE_VERSION}-linux-${TARGETARCH}"; \
    curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-${oc_arch}.tar.gz" -o /tmp/opencode.tar.gz; \
    echo "${oc_sha}  /tmp/opencode.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/opencode.tar.gz -C /tmp opencode; \
    install -m 0755 /tmp/opencode /usr/local/bin/opencode; \
    rm -f /tmp/opencode /tmp/opencode.tar.gz; \
    curl -fsSL "https://raw.githubusercontent.com/newren/git-filter-repo/v2.47.0/git-filter-repo" -o /tmp/git-filter-repo; \
    echo "${GIT_FILTER_REPO_SHA256}  /tmp/git-filter-repo" | sha256sum -c -; \
    install -m 0755 /tmp/git-filter-repo /usr/local/bin/git-filter-repo; \
    rm -f /tmp/git-filter-repo; \
    git filter-repo --version >/dev/null

RUN mkdir -p /secrets /work \
 && git config --system --add safe.directory '*' \
 && git config --system init.defaultBranch main \
 && git config --system user.name 'aistorage-spike' \
 && git config --system user.email 'spike@aistorage.invalid'
ENV HOME=/work
WORKDIR /work
CMD ["bash", "-l"]
