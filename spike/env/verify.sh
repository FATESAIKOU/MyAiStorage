#!/usr/bin/env bash
# 技術驗證：容器環境的驗證腳本（在 Mac 上執行）。
# 產出 evidence 到 spike/evidence/env-<arch>.txt，同時印到 stdout。
# 不建立任何 Drive / GitHub 資源；用的是暫時的假秘密檔，不碰使用者的真憑證。
#
# 預設驗證 colima server 架構（Apple Silicon 是 arm64）。要另外驗 amd64 image：
#   ARCH=amd64 IMAGE=aistorage-spike-env:amd64 spike/env/verify.sh
# （在 Apple Silicon 上是 qemu 模擬；git-annex 在此環境可能 segfault，見 README。）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN="${HERE}/run.sh"

host_arch="$(docker version --format '{{.Server.Arch}}')"
arch="${ARCH:-${host_arch}}"
IMAGE="${AISTORAGE_SPIKE_IMAGE:-${IMAGE:-aistorage-spike-env:latest}}"
if [ "${arch}" != "${host_arch}" ]; then
  export AISTORAGE_SPIKE_PLATFORM="linux/${arch}"
  echo "注意：${arch} 與 colima server arch（${host_arch}）不同，是模擬執行" >&2
fi

tmp_root="$(mktemp -d "${HOME}/.cache/aistorage-spike-verify.XXXXXX")"
cleanup() {
  chmod -R u+rwX "${tmp_root}" 2>/dev/null || true
  rm -rf "${tmp_root}" 2>/dev/null || true
}
trap cleanup EXIT
fake_secrets="${tmp_root}/secrets"
fake_work="${tmp_root}/work"
mkdir -p "${fake_secrets}" "${fake_work}"
printf 'dummy-one\n' > "${fake_secrets}/spike-dummy-a.txt"
printf 'dummy-two\n' > "${fake_secrets}/spike-dummy-b.txt"
printf 'dummy-three\n' > "${fake_secrets}/spike-dummy-c.txt"
chmod 600 "${fake_secrets}"/*.txt

arch="$(docker image inspect "${IMAGE}" --format '{{.Architecture}}' 2>/dev/null || echo "${ARCH:-${host_arch}}")"
evidence="${HERE}/../evidence/env-${arch}.txt"
mkdir -p "$(dirname "${evidence}")"

export AISTORAGE_SPIKE_SECRETS="${fake_secrets}"
export AISTORAGE_SPIKE_WORK_ROOT="${fake_work}"
export AISTORAGE_SPIKE_IMAGE="${IMAGE}"

run_in() {
  local name="$1"; shift
  "${RUN}" "${name}" "$@" 2>&1
}

{
  echo "# 技術驗證：容器環境（tasks 1.2 的前置）"
  echo "# 執行時間：$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
  echo "# 平台：macOS $(sw_vers -productVersion) / colima server arch=${host_arch} / image arch=$(docker image inspect "${IMAGE}" --format '{{.Architecture}}' 2>/dev/null || echo '?')"
  if [ -n "${AISTORAGE_SPIKE_PLATFORM:-}" ]; then
    echo "# 注意：image 與 colima server 架構不同（${AISTORAGE_SPIKE_PLATFORM}），是 qemu 模擬執行"
  fi
  echo
  echo "## 1. 工具版本與 git-remote-annex"
  echo "\$ docker run --rm ${IMAGE} bash -lc '...'"
  run_in verify-tools -- bash -lc '
    echo "--- git annex version"
    git annex version > /tmp/annex-version.txt 2>&1; echo "rc=$?"; head -1 /tmp/annex-version.txt
    echo "--- git-remote-annex 位置"
    command -v git-remote-annex || echo "NOT FOUND"
    echo "--- rclone version"
    rclone version | head -1
    echo "--- opencode --version"
    opencode --version
    echo "--- sqlite3 --version"
    sqlite3 --version | cut -d" " -f1
    echo "--- jq --version"
    jq --version
    echo "--- python3 --version"
    python3 --version
    echo "--- git --version"
    git --version
  '
  echo
  echo "## 2. sqlite FTS5 trigram（中文查詢）"
  run_in verify-fts -- bash -lc '
    sqlite3 :memory: "CREATE VIRTUAL TABLE t USING fts5(x, tokenize='"'"'trigram'"'"'); INSERT INTO t(x) VALUES ('"'"'人工智慧儲存層'"'"'); SELECT x FROM t WHERE t MATCH '"'"'智慧儲存'"'"';"
    echo "FTS5 trigram ok (rc=$?)"
    sqlite3 :memory: "PRAGMA compile_options;" | grep FTS || true
  '
  echo
  echo "## 3. /secrets 白名單（只掛列出的檔）"
  echo "\$ spike/env/run.sh verify-secrets spike-dummy-a.txt spike-dummy-b.txt"
  echo "（spike-dummy-c.txt 存在於秘密目錄 但沒有列在命令列，所以不該進容器：）"
  ls -l "${fake_secrets}/spike-dummy-c.txt"
  run_in verify-secrets spike-dummy-a.txt spike-dummy-b.txt -- bash -lc '
    echo "ls -la /secrets/"; ls -la /secrets/
    echo "--- 未列出的秘密檔（spike-dummy-c.txt）:"
    ls /secrets/spike-dummy-c.txt 2>&1 || true
    echo "--- 掛載是唯讀的："
    (echo x >> /secrets/spike-dummy-a.txt) 2>&1 || true
  '
  echo
  echo "## 4. 不掛秘密時 /secrets 是空的"
  run_in verify-empty -- bash -lc 'ls -la /secrets/'
  echo
  echo "## 5. 容器看不到 Mac 的 home 內容"
  run_in verify-isolation -- bash -lc '
    echo "HOME=$HOME"; pwd
    for p in /Users /Users/fatesaikou/.ssh /Users/fatesaikou/.config/gh /root/.ssh /root/.config; do
      if [ -e "$p" ]; then echo "存在：$p"; else echo "不存在：$p"; fi
    done
    echo "--- 容器內 home:"
    ls -la "$HOME" 2>&1
  '
  echo
  echo "## 6. 每個容器的工作目錄彼此不共用"
  echo "\$ run.sh verify-work-a / verify-work-b"
  run_in verify-work-a -- bash -lc 'echo from-a > /work/marker-a.txt; ls /work'
  run_in verify-work-b -- bash -lc 'ls /work; test ! -e /work/marker-a.txt && echo "b 看不到 a 的檔案"'
  echo
  echo "## 7. run.sh 拒絕不在白名單的檔名 / 不合法名稱"
  ( set +e; "${RUN}" verify-bad ../etc/passwd </dev/null; echo "rc=$?" ) 2>&1
  ( set +e; "${RUN}" 'bad name' </dev/null; echo "rc=$?" ) 2>&1
  ( set +e; "${RUN}" verify-missing nonexistent-secret.txt </dev/null; echo "rc=$?" ) 2>&1
  echo
  echo "## 8. git-annex 在 /work（sshfs 掛載）上的 roundtrip（1.2 的前置）"
  echo "\$ git init / git-annex initremote / push / clone（directory remote 放 /tmp）"
  roundtrip_rc=0
  run_in verify-annex -- bash -lc '
    set -e
    mkdir -p /tmp/verify-remote /work/src
    cd /work/src
    git init -q -b main .
    echo hello-annex > a.txt
    git add a.txt && git commit -qm first
    git annex init src >/dev/null 2>&1
    git annex initremote smoke type=directory directory=/tmp/verify-remote encryption=none --with-url >/dev/null 2>&1
    git push smoke main 2>&1 | tail -1
    uuid=$(git annex info smoke --fast 2>/dev/null | awk "/^uuid:/{print \$2}")
    cd /work
    git clone "annex::${uuid}?encryption=none&type=directory&directory=/tmp/verify-remote" /work/clone 2>&1 | tail -1
    cat /work/clone/a.txt
    git -C /work/clone log --oneline | head -1
    echo "git-annex roundtrip ok（uid=$(id -u)）"
  ' || roundtrip_rc=$?
  if [ "${roundtrip_rc}" -ne 0 ]; then
    echo "!! git-annex roundtrip 失敗（rc=${roundtrip_rc}）：$( [ -n "${AISTORAGE_SPIKE_PLATFORM:-}" ] && echo '這一組是 qemu 模擬執行，可能是模擬問題' || echo '需要人工看上面輸出' )"
  fi
  echo
  echo "## 9. 能力邊界（給 1.7 / 5.1 的前置檢查）"
  echo "容器內看不到白名單以外的憑證：env / /proc/mounts / docker.sock / privileged / capabilities"
  run_in verify-boundary spike-dummy-a.txt -- bash -lc '
    echo "--- env（只看得到我們注入的白名單變數）:"
    env | sort
    echo "--- /proc/mounts（來源路徑只該有 /secrets/* 與 /work）:"
    grep -E " (/secrets|/work)" /proc/mounts || echo "(沒有 /secrets 或 /work 掛載)"
    echo "--- docker.sock:"
    ls -la /var/run/docker.sock /run/docker.sock 2>&1 || true
    echo "--- privileged:"
    grep -E "^Cap(Inh|Prm|Eff|Bnd|Amb):" /proc/self/status
    echo "--- capabilities（capsh 不一定有，改看 CapEff 與裝置）:"
    ls /dev | head -30
  '
  echo
  echo "# 完成（roundtrip rc=${roundtrip_rc}）"
} | tee "${evidence}"

if grep -q '^!! ' "${evidence}"; then
  echo
  echo "有失敗項目，見上面與 ${evidence}" >&2
  exit 1
fi

echo
echo "evidence 已寫入：${evidence}"
