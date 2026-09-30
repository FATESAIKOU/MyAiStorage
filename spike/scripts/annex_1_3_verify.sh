#!/usr/bin/env bash
# 1.3 查驗：對給定的 clone / bundle 集合搜 canary。
# 用法：bash annex_1_3_verify.sh <part> <workdir> <prefix> <canary>
#   part = precheck | bundlecheck | postcheck
#
# bundlecheck：只做「依 GITMANIFEST 順序 unbundle 全部 bundle」這一段，獨立可測。
#   任何一顆 bundle 解不開 → 「查驗無效」exit 1（不得當成無命中）。全部解開才繼續搜 canary。
set -euo pipefail

part="${1:?}"; workdir="${2:?}"; prefix="${3:?}"; canary="${4:?}"
export HOME="${HOME:-/work}"
[ -n "${RCLONE_CONFIG:-}" ] || { echo "RCLONE_CONFIG 未設" >&2; exit 2; }
log() { printf '\n### %s\n' "$*"; }

# 依 GITMANIFEST（首選；沒有就用 .bak）的順序把全部 GITBUNDLE unbundle 進同一個 repo，
# 再搜 canary。回傳 0＝查驗有效（並印出命中／無命中）；1＝查驗無效（bundle 解不開或清單不全）。
verify_bundles() {
  local bundles_dir="/work/bundles"
  rm -rf "${bundles_dir}"; mkdir -p "${bundles_dir}"
  for name in $(rclone lsf --files-only "gdrive:${prefix}/" 2>/dev/null | grep '^GITBUNDLE'); do
    rclone copyto "gdrive:${prefix}/${name}" "${bundles_dir}/${name}" 2>/dev/null
  done
  echo "   bundle 檔數：$(ls "${bundles_dir}" 2>/dev/null | grep -c GITBUNDLE || echo 0)"

  # 取 GITMANIFEST 的內容作為順序來源（增量 bundle 有 prerequisite，順序錯就解不開）
  local manifest_src=""
  for cand in $(rclone lsf --files-only "gdrive:${prefix}/" 2>/dev/null | grep 'GITMANIFEST' | grep -v '\.bak$'); do
    manifest_src="${cand}"; break
  done
  if [ -z "${manifest_src}" ]; then
    for cand in $(rclone lsf --files-only "gdrive:${prefix}/" 2>/dev/null | grep 'GITMANIFEST.*\.bak$'); do
      manifest_src="${cand}"; break
    done
  fi

  local invalid=0
  if [ -n "${manifest_src}" ]; then
    echo "   manifest：${manifest_src##*/}"
    rclone cat "gdrive:${prefix}/${manifest_src}" 2>/dev/null | grep -v '^-' | sed '/^$/d' > "${bundles_dir}/_manifest.txt" || true
  fi
  local manifest_names=""
  [ -s "${bundles_dir}/_manifest.txt" ] && manifest_names="$(sed 's/ *$//' "${bundles_dir}/_manifest.txt")"

  rm -rf "${bundles_dir}/_chain"; mkdir -p "${bundles_dir}/_chain"
  git -C "${bundles_dir}/_chain" init -q -b main .
  if [ -n "${manifest_names}" ]; then
    echo "   --- 依 manifest 順序（共 $(printf '%s\n' "${manifest_names}" | grep -c .) 顆）逐顆 unbundle ---"
    while IFS= read -r bn; do
      [ -n "${bn}" ] || continue
      [ -f "${bundles_dir}/${bn}" ] || { echo "   manifest 列的 bundle 不存在：${bn}（查驗無效）"; invalid=1; continue; }
      if git -C "${bundles_dir}/_chain" bundle unbundle "${bundles_dir}/${bn}" >/dev/null 2>&1; then
        echo "   ok：${bn##*/}"
      else
        echo "   解不開（查驗無效）：${bn##*/}"
        invalid=1
      fi
    done <<< "${manifest_names}"
  else
    echo "   ⚠ 取不到 GITMANIFEST 清單（查驗無效）"
    invalid=1
  fi

  if [ "${invalid}" -eq 1 ]; then
    echo "   ✗ 有 bundle 無法解開或清單不完整：本次查驗無效（不得當成無命中）"
    return 1
  fi

  echo "   累積物件數：$(git -C "${bundles_dir}/_chain" count-objects -v 2>/dev/null | awk '/in-pack/{print $2; exit}')"
  # 注意：不可用 `git cat-file … | grep -q`——grep -q 找到就提早結束，cat-file 收到 SIGPIPE
  # 會被 pipefail 判成管線失敗，讓「有命中」被誤判成「無命中」（假陰性）。先落檔再 grep。
  git -C "${bundles_dir}/_chain" cat-file --batch-all-objects --batch > "${bundles_dir}/_objects.bin" 2>/dev/null || true
  if grep -qF "${canary}" "${bundles_dir}/_objects.bin"; then
    echo "   命中 canary（manifest 鏈）"
  else
    echo "   無命中（manifest 鏈）"
  fi

  # 交叉檢查：逐一獨立 unbundle（解不開的只記錄，不改變判定——增量 bundle 本來就需要鏈）
  echo "   --- 交叉檢查：逐一獨立 unbundle ---"
  for b in "${bundles_dir}"/GITBUNDLE*; do
    [ -e "${b}" ] || continue
    rm -rf "${bundles_dir}/_one"; mkdir -p "${bundles_dir}/_one"
    git -C "${bundles_dir}/_one" init -q -b main .
    if git -C "${bundles_dir}/_one" bundle unbundle "${b}" >/dev/null 2>&1; then
      git -C "${bundles_dir}/_one" cat-file --batch-all-objects --batch > "${bundles_dir}/_one.bin" 2>/dev/null || true
      if grep -qF "${canary}" "${bundles_dir}/_one.bin"; then
        echo "   命中 canary：${b##*/}"
      else
        echo "   無命中：${b##*/}"
      fi
    else
      echo "   （獨立解不開，需依 manifest 鏈：${b##*/}）"
    fi
  done

  echo "   annex 物件以 key（SHA256）確認："
  rclone lsf --files-only "gdrive:${prefix}/" 2>/dev/null | grep '^SHA256E' | sed 's/^/     /' || echo "     （無 annex 物件）"
  return 0
}

case "${part}" in
  precheck)
    log "0) 抹除前：確認 canary 在現行版本、歷史與 annex 物件中都找得到（防假陰性）"
    cd "${workdir}"
    echo "--- 現行版本工作樹 grep ---"
    grep -rlF "${canary}" . --exclude-dir=.git 2>/dev/null | sed 's/^/  /' || echo "  （無）"
    echo "--- git grep HEAD ---"
    git grep -lF "${canary}" HEAD 2>/dev/null | sed 's/^/  /' || echo "  （無）"
    echo "--- git log --all -S canary ---"
    git log --all -S "${canary}" --oneline 2>/dev/null | sed 's/^/  /' || echo "  （無）"
    echo "--- rev-list --all --objects 中的檔名 ---"
    git rev-list --all --objects | grep -E 'erase-canary' | sed 's/^/  /' || echo "  （無）"
    echo "--- annex 物件（本機） ---"
    find .git/annex/objects -type f | head -5 | while read -r f; do
      if grep -qF "${canary}" "$f" 2>/dev/null; then echo "  命中：${f##*/}"; fi
    done || true
    echo "--- remote 上的 annex 物件（取回後 grep）---"
    for name in $(rclone lsf --files-only "gdrive:${prefix}/" 2>/dev/null | grep '^SHA256E'); do
      tmpf="$(mktemp)"
      rclone cat "gdrive:${prefix}/${name}" > "${tmpf}" 2>/dev/null
      if grep -qF "${canary}" "${tmpf}"; then echo "  命中 remote 物件：${name}"; fi
      rm -f "${tmpf}"
    done
    ;;

  bundlecheck)
    log "1) bundle 查驗（只做 bundle 段）"
    if verify_bundles; then
      echo
      echo "### part=${part} 完成（查驗有效 rc=0）"
    else
      echo
      echo "### part=${part} 完成（查驗無效 rc=1）"
      exit 1
    fi
    ;;

  postcheck)
    log "1) 抹除後查驗"
    echo "=== a. 現行版本（全新 clone）==="
    cd /work
    chmod -R u+w "${workdir}-verify" 2>/dev/null || true
    rm -rf "${workdir}-verify"
    if ! git clone -b main "$(cat "${workdir}.url")" "${workdir}-verify" 2>&1 | tail -1; then
      echo "   ✗ clone 失敗：無法查驗現行版本（查驗無效）"
      echo
      echo "### part=${part} 完成（查驗無效 rc=1）"
      exit 1
    fi
    cd "${workdir}-verify"
    git annex init verify >/dev/null 2>&1 || true
    echo "   工作樹檔案:"; ls -la | sed 's/^/   /'
    echo "   grep -r 工作樹 canary:"
    grep -rF "${canary}" . --exclude-dir=.git 2>/dev/null | sed 's/^/   /' || echo "   （無命中）"
    echo "   git grep HEAD canary:"
    git grep -F "${canary}" HEAD 2>/dev/null | sed 's/^/   /' || echo "   （無命中）"

    echo
    echo "=== b. git 歷史（clone 內）==="
    echo "   git log --all -S canary -p:"
    git log --all -S "${canary}" -p 2>/dev/null | head -5 | sed 's/^/   /' || true
    echo "   （以上無輸出＝無命中）"
    echo "   rev-list --all --objects 含 erase-canary 的項數:"
    git rev-list --all --objects 2>/dev/null | grep -c 'erase-canary' || echo "   0"
    echo "   git annex unused / fsck:"
    git annex unused 2>&1 | tail -3 | sed 's/^/   /' || true
    git annex fsck --fast 2>&1 | tail -3 | sed 's/^/   /' || true
    echo "   annex 物件（**只掃這個 clone 內**）含 canary 的檔數:"
    found=0
    while IFS= read -r f; do
      if grep -qF "${canary}" "$f" 2>/dev/null; then echo "   命中：$f"; found=1; fi
    done < <(find "${PWD}/.git/annex/objects" -type f 2>/dev/null)
    [ "${found}" -eq 1 ] && echo "   ⚠ 有命中" || echo "   0（無命中）"

    echo
    echo "=== c. bundle（依 GITMANIFEST 順序 unbundle，任何 bundle 解不開即判查驗無效）==="
    cd /work
    if ! verify_bundles; then
      echo
      echo "### part=${part} 完成（查驗無效 rc=1）"
      exit 1
    fi
    ;;

  *)
    echo "unknown part: ${part}" >&2; exit 2 ;;
esac
echo
echo "### part=${part} 完成"
