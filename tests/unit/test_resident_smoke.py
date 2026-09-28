"""住民容器（tasks 5.1）的冒煙測試（實作方撰寫；驗收由測試方另寫）。

不啟動 docker、不跑 LLM。只驗三件事：
1. 白名單：目錄裡有不在白名單的檔名就拒絕；只掛所選 provider 的金鑰。
2. 產生的 opencode.json：金鑰是 `{file:/secrets/...}` 檔案引用，不是明文。
3. 邊界腳本與 Dockerfile/run.sh 沒有把不該有的東西帶進容器
   （docker.sock、--privileged、--cap-add、Claude 憑證）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[2]
RESIDENT = REPO / "resident"
RUN_SH = RESIDENT / "run.sh"
ENTRYPOINT = RESIDENT / "image" / "entrypoint.sh"
VERIFY_SH = RESIDENT / "verify-boundary.sh"
DOCKERFILE = RESIDENT / "image" / "Dockerfile"
BASE_JSON = RESIDENT / "opencode" / "opencode.base.json"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="需要 bash 與 jq（entrypoint 用 jq 產生設定）",
)

WHITELIST = {
    "rclone-worker.conf",
    "sa-reader.json",
    "signing.key",
    "reader.json",
    "gh-pat-actions.txt",
    "llm-opencode.key",
}


def _profile_dir(tmp_path: Path, names: set[str] | None = None) -> Path:
    d = tmp_path / "resident" / "mac-opencode"
    d.mkdir(parents=True, exist_ok=True)
    for name in (WHITELIST if names is None else names):
        p = d / name
        p.write_text("placeholder-not-a-secret\n")
        os.chmod(p, 0o600)
    return d


def _plan(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(
        os.environ,
        AISTORAGE_RESIDENT_ROOT=str(tmp_path / "resident"),
        AISTORAGE_WORK_ROOT=str(tmp_path / "work"),
    )
    return subprocess.run(
        ["bash", str(RUN_SH), *args],
        capture_output=True, text=True, env=env, check=False,
    )


def _default_plan(tmp_path: Path, name: str = "c1", **kw) -> dict:
    args = [name, "--profile", "mac-opencode",
            "--model", "opencode/space-bunny-free", "--print-plan"]
    for flag, value in kw.items():
        flag = "--" + flag.replace("_", "-")
        args += [flag] if value is True else [flag, value]
    proc = _plan(tmp_path, *args)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# ── 1. 白名單 ───────────────────────────────────────────────────────────


def test_plan_mounts_only_whitelisted_files(tmp_path: Path):
    _profile_dir(tmp_path)
    plan = _default_plan(tmp_path)
    assert set(plan["secrets_mounted"]) <= WHITELIST
    assert {"rclone-worker.conf", "signing.key", "llm-opencode.key"} <= set(
        plan["secrets_mounted"])
    assert plan["home"] == "/work"
    assert plan["work_dir"].endswith("/work/c1")
    assert plan["user"] == f"{os.getuid()}:{os.getgid()}"
    assert plan["docker_sock"] is False and plan["privileged"] is False
    assert plan["cap_add"] == []


def test_non_whitelisted_file_refuses_to_start(tmp_path: Path):
    """目錄裡有白名單外的檔名 → 拒絕啟動並指名，而不是默默忽略。"""
    d = _profile_dir(tmp_path)
    (d / ".env").write_text("SECRET=1\n")
    os.chmod(d / ".env", 0o600)
    proc = _plan(tmp_path, "c1", "--profile", "mac-opencode",
                 "--model", "opencode/space-bunny-free", "--print-plan")
    assert proc.returncode != 0
    assert ".env" in proc.stderr
    assert "白名單" in proc.stderr


def test_claude_credentials_are_not_whitelisted(tmp_path: Path):
    d = _profile_dir(tmp_path)
    for name in ("anthropic.key", "claude-api-key.txt", ".claude.json"):
        (d / name).write_text("nope\n")
        os.chmod(d / name, 0o600)
    proc = _plan(tmp_path, "c1", "--profile", "mac-opencode",
                 "--model", "opencode/space-bunny-free", "--print-plan")
    assert proc.returncode != 0
    for name in ("anthropic.key", "claude-api-key.txt", ".claude.json"):
        assert name in proc.stderr


def test_only_selected_provider_key_is_mounted(tmp_path: Path):
    d = _profile_dir(tmp_path)
    (d / "llm-ollama.key").write_text("placeholder-not-a-secret\n")
    os.chmod(d / "llm-ollama.key", 0o600)
    plan = _default_plan(tmp_path)
    assert "llm-opencode.key" in plan["secrets_mounted"]
    assert "llm-ollama.key" not in plan["secrets_mounted"]


def test_missing_required_file_refuses_to_start(tmp_path: Path):
    _profile_dir(tmp_path, names=WHITELIST - {"signing.key"})
    proc = _plan(tmp_path, "c1", "--profile", "mac-opencode",
                 "--model", "opencode/space-bunny-free", "--print-plan")
    assert proc.returncode != 0 and "signing.key" in proc.stderr


def test_loose_permissions_refuse_to_start(tmp_path: Path):
    d = _profile_dir(tmp_path)
    os.chmod(d / "rclone-worker.conf", 0o644)
    proc = _plan(tmp_path, "c1", "--profile", "mac-opencode",
                 "--model", "opencode/space-bunny-free", "--print-plan")
    assert proc.returncode != 0
    assert "chmod 600" in proc.stderr


def test_container_name_and_model_are_validated(tmp_path: Path):
    _profile_dir(tmp_path)
    bad_name = _plan(tmp_path, "bad name!", "--profile", "mac-opencode",
                     "--model", "opencode/space-bunny-free", "--print-plan")
    assert bad_name.returncode != 0
    bad_model = _plan(tmp_path, "c1", "--profile", "mac-opencode",
                      "--model", "space-bunny-free", "--print-plan")
    assert bad_model.returncode != 0 and "provider" in bad_model.stderr


def test_publish_api_is_off_by_default(tmp_path: Path):
    _profile_dir(tmp_path)
    assert _default_plan(tmp_path)["publish_api"] is None
    # 計畫裡是 JSON 數字（埠號）或 null
    assert _default_plan(tmp_path, name="c2", publish_api="4096")["publish_api"] == 4096


# ── 2. opencode.json：檔案引用而非明文 ───────────────────────────────────


def _render(tmp_path: Path, provider: str, model: str) -> subprocess.CompletedProcess:
    secrets = tmp_path / "secrets"
    secrets.mkdir(exist_ok=True)
    (secrets / f"llm-{provider}.key").write_text("placeholder-not-a-secret\n")
    return subprocess.run(
        ["bash", str(ENTRYPOINT), "render-config", provider, model, str(BASE_JSON)],
        capture_output=True, text=True, check=False,
        env=dict(os.environ, AISTORAGE_SECRETS_DIR=str(secrets)),
    )


def test_generated_config_uses_file_reference_for_the_key(tmp_path: Path):
    proc = _render(tmp_path, "opencode", "opencode/space-bunny-free")
    assert proc.returncode == 0, proc.stderr
    cfg = json.loads(proc.stdout)
    assert cfg["model"] == "opencode/space-bunny-free"
    assert cfg["provider"]["opencode"]["options"]["apiKey"] == \
        "{file:/secrets/llm-opencode.key}"
    # 檔案裡**沒有**金鑰內容，也沒有任何秘密
    assert "placeholder-not-a-secret" not in proc.stdout
    # plugin 與 skill 說明來自唯讀的 /opt/aistorage
    assert cfg["plugin"] == ["/opt/aistorage/opencode/plugin/aistorage.ts"]
    assert cfg["skills"]["paths"] == ["/opt/aistorage/opencode/skills"]
    # opencode 不得讀寫 /secrets（工具層的權限）
    assert cfg["permission"]["external_directory"]["/secrets/**"] == "deny"


def test_generated_config_refuses_mismatched_provider(tmp_path: Path):
    proc = _render(tmp_path, "opencode", "ollama/some-model")
    assert proc.returncode != 0
    assert "provider" in proc.stderr


def test_base_template_has_no_secret_and_no_claude(tmp_path: Path):
    raw = BASE_JSON.read_text(encoding="utf-8")
    cfg = json.loads(raw)
    assert set(cfg["provider"]) == {"__PROVIDER__"}
    assert cfg["provider"]["__PROVIDER__"]["options"]["apiKey"] == "__API_KEY_FILE__"
    assert "__MODEL__" == cfg["model"]
    for word in ("anthropic", "claude", "sk-"):
        assert word not in raw.lower()


def test_entrypoint_refuses_claude_credentials(tmp_path: Path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "llm-opencode.key").write_text("x\n")
    (secrets / "rclone-worker.conf").write_text("[gdrive]\n")
    (secrets / "signing.key").write_text("y\n")
    work = tmp_path / "work"
    work.mkdir()
    (work / ".claude.json").write_text("{}")
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT), "serve"],
        capture_output=True, text=True, check=False,
        env=dict(
            os.environ,
            AISTORAGE_SECRETS_DIR=str(secrets),
            HOME=str(work),
            AISTORAGE_MODEL="opencode/space-bunny-free",
            AISTORAGE_RESIDENT_NO_TUI="1",
        ),
    )
    assert proc.returncode != 0
    assert "拒絕啟動" in proc.stderr


def test_entrypoint_refuses_env_auth_json(tmp_path: Path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "llm-opencode.key").write_text("x\n")
    (secrets / "rclone-worker.conf").write_text("[gdrive]\n")
    (secrets / "signing.key").write_text("y\n")
    proc = subprocess.run(
        ["bash", str(ENTRYPOINT), "serve"],
        capture_output=True, text=True, check=False,
        env=dict(
            os.environ,
            AISTORAGE_SECRETS_DIR=str(secrets),
            HOME=str(tmp_path),
            AISTORAGE_MODEL="opencode/space-bunny-free",
            ANTHROPIC_API_KEY="sk-not-a-real-key",
        ),
    )
    assert proc.returncode != 0
    assert "ANTHROPIC_API_KEY" in proc.stderr


# ── 3. 邊界：腳本與 image 不該帶的東西 ───────────────────────────────────


@pytest.mark.parametrize(
    "path", [RUN_SH, ENTRYPOINT, VERIFY_SH, RESIDENT / "build.sh"], ids=lambda p: p.name
)
def test_shell_scripts_parse(path: Path):
    proc = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def _code_lines(path: Path) -> str:
    """去掉註解行，只看真的會被執行的內容（說明文字可以提到 claude）。"""
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        out.append(line.split("  #")[0] if "  #" in line else line)
    return "\n".join(out)


def test_run_script_never_asks_for_extra_privileges():
    text = _code_lines(RUN_SH)
    for forbidden in ("--privileged", "--cap-add", "docker.sock", "/var/run/docker"):
        assert forbidden not in text, f"run.sh 出現不該有的字串：{forbidden}"
    assert "--user" in text  # 用 Mac 的 uid:gid
    assert "readonly" in text  # /secrets 一律唯讀


def test_dockerfile_has_no_claude_and_no_privileges():
    text = _code_lines(DOCKERFILE).lower()
    for forbidden in ("anthropic", "claude", "docker.sock", "privileged"):
        assert forbidden not in text
    assert "home=/work" in text


def test_secrets_dir_is_traversable_but_not_listable():
    """實測教訓：/secrets 若是 root 的 0700，uid 501 連 stat 都做不到，
    entrypoint 會誤判「缺少必要檔案」。0711 = 可穿越、不可列目錄。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "chmod 0711 /secrets" in text
    assert "chmod 0700 /secrets" not in text


def test_serve_readiness_probe_has_a_timeout():
    """實測教訓：serve 剛起來時 /session 可能連得上卻不回應，
    沒有 --max-time 的 curl 會把 entrypoint 卡死。"""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "--max-time" in text and "--connect-timeout" in text
    assert "沒有就緒" in text


def test_missing_syncer_does_not_kill_the_container():
    """5.2 還沒實作時，同步器失敗只能記警告，不能讓容器退出。"""
    text = ENTRYPOINT.read_text(encoding="utf-8")
    assert "同步器沒有跑起來" in text
    assert "容器繼續提供 serve" in text


def test_container_build_pins_a_working_cryptography():
    """實測教訓：cryptography 47+ 的 aarch64 wheel 在 colima VM 上 SIGILL。"""
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "cryptography>=42,<47" in text


def test_build_script_passes_platform_and_targetarch():
    """colima 沒有 buildx（legacy builder 不帶 TARGETARCH），而且本機的
    ubuntu:24.04 標籤曾快取成 amd64 → 兩個都要明確傳。"""
    text = (RESIDENT / "build.sh").read_text(encoding="utf-8")
    assert "--build-arg" in text and "TARGETARCH" in text
    assert "--platform" in text


def test_verify_boundary_checks_the_required_items():
    text = VERIFY_SH.read_text(encoding="utf-8")
    for needle in ("/proc/mounts", "docker.sock", "CapEff", "/Users",
                   "secrets_writable", "secret_leak_hits", "grep -r -c -F -f",
                   "ANTHROPIC", "auth.json"):
        assert needle in text, f"verify-boundary.sh 少了：{needle}"


def test_leak_scan_ignores_trivially_short_lines(tmp_path: Path):
    """`grep -F -f` 會把 pattern 檔裡的短行也拿去比對。

    JSON／conf 裡有 `}`、`},` 這種 1～2 字元的行；不濾掉���話，/work 底下
    opencode 裝的 node_modules 會讓幾千個檔案「命中」，檢查失去訊號。
    """
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    # 檔名必須在白名單裡（腳本照白名單指名，不用 glob）
    (secrets / "sa-reader.json").write_text(
        '{\n  "type": "service_account",\n  "private_key_id": "0123456789abcdef",\n}\n',
        encoding="utf-8",
    )
    (secrets / "signing.key").write_text("x" * 4 + "\n", encoding="utf-8")
    (secrets / "llm-opencode.key").write_text("sk-placeholder-value-000000\n", encoding="utf-8")
    work = tmp_path / "work"
    (work / "node_modules").mkdir(parents=True)
    # 只有 JSON 標點相同，沒有任何秘密的實質內容
    (work / "node_modules" / "package.json").write_text(
        '{\n  "name": "zod",\n}\n', encoding="utf-8"
    )
    proc = subprocess.run(
        ["bash", str(VERIFY_SH), "--json"],
        capture_output=True, text=True, check=False,
        env=dict(os.environ, AISTORAGE_SECRETS_DIR=str(secrets),
                 AISTORAGE_WORK_ROOT=str(work)),
    )
    assert proc.returncode is not None
    data = json.loads(proc.stdout)
    assert data["secret_leak_scanned"] == 3, proc.stdout
    assert data["secret_leak_hits"] == 0, proc.stdout


def test_leak_scan_skips_reader_json_which_is_not_a_secret(tmp_path: Path):
    """reader.json 是刻意放在 /secrets 的**非秘密**設定。

    掃它只會假警報：它裡面是 `  "inbox_folder_ids": {` 這種通用 JSON 鍵，
    會撞到 /work 底下任何一個 JSON（實測撞到 schemas/identity-registry）。
    """
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "reader.json").write_text(
        '{\n  "format": "aistorage.reader/v1",\n  "inbox_folder_ids": {\n'
        '    "mac-opencode": "0AbCdEfGhIjKlMnOpQrStUvWxYz"\n  }\n}\n',
        encoding="utf-8",
    )
    work = tmp_path / "work"
    work.mkdir()
    (work / "schema.json").write_text(
        '{\n  "type": "object",\n  "required": [\n    "inbox_folder_ids"\n  ]\n}\n',
        encoding="utf-8",
    )
    proc = subprocess.run(
        ["bash", str(VERIFY_SH), "--json"],
        capture_output=True, text=True, check=False,
        env=dict(os.environ, AISTORAGE_SECRETS_DIR=str(secrets),
                 AISTORAGE_WORK_ROOT=str(work)),
    )
    data = json.loads(proc.stdout)
    # 只有 reader.json → 一個秘密都沒掃到 → 檢查無效，必須失敗（不是「0 命中」）
    assert data["secret_leak_scanned"] == 0, proc.stdout
    assert proc.returncode != 0
    assert "檢查無效" in data["secret_leak_detail"]


def test_leak_scan_fails_when_no_secret_file_can_be_read(tmp_path: Path):
    """`/secrets` 是 0711（不可列目錄）→ glob 不會展開。

    那時迴圈一個檔案都沒掃到，`secret_leak_hits` 會是 0（假的通過）。
    腳本必須自己判定「沒掃到就是沒檢查」並失敗。
    """
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "signing.key").write_text("y" * 32 + "\n", encoding="utf-8")
    secrets.chmod(0o711)          # 可穿越、不可列目錄
    work = tmp_path / "work"
    work.mkdir()
    (work / "clean.txt").write_text("沒有秘密\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(VERIFY_SH), "--json"],
        capture_output=True, text=True, check=False,
        env=dict(os.environ, AISTORAGE_SECRETS_DIR=str(secrets),
                 AISTORAGE_WORK_ROOT=str(work)),
    )
    data = json.loads(proc.stdout)
    # 目錄不可列，但已知路徑仍讀得到 → 掃到 1 個 → 檢查有效、0 命中
    assert data["secret_leak_scanned"] == 1, proc.stdout
    assert data["secret_leak_hits"] == 0
    secrets.chmod(0o700)

    # 真的什麼都讀不到（檔案不存在）→ 掃過 0 個 → 必須失敗
    shutil.rmtree(secrets)
    proc2 = subprocess.run(
        ["bash", str(VERIFY_SH), "--json"],
        capture_output=True, text=True, check=False,
        env=dict(os.environ, AISTORAGE_SECRETS_DIR=str(secrets),
                 AISTORAGE_WORK_ROOT=str(work)),
    )
    data2 = json.loads(proc2.stdout)
    assert data2["secret_leak_scanned"] == 0, proc2.stdout
    assert proc2.returncode != 0, "沒掃到秘密檔時不該回報通過"


def test_leak_scan_still_catches_a_real_leak(tmp_path: Path):
    """濾掉短行不能把真正的洩漏也濾掉。"""
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    token = "ya29.a0AfH6SMBexampletokenvalue0123456789"
    # 檔名必須在白名單裡：腳本不再用 glob（/secrets 不可列目錄）
    (secrets / "signing.key").write_text(token + "\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "leaked.log").write_text(f"Authorization: Bearer {token}\n", encoding="utf-8")
    proc = subprocess.run(
        ["bash", str(VERIFY_SH), "--json"],
        capture_output=True, text=True, check=False,
        env=dict(os.environ, AISTORAGE_SECRETS_DIR=str(secrets),
                 AISTORAGE_WORK_ROOT=str(work)),
    )
    data = json.loads(proc.stdout)
    assert data["secret_leak_scanned"] == 1, proc.stdout
    assert data["secret_leak_hits"] >= 1, proc.stdout
    assert proc.returncode != 0, "偵測到洩漏時應該讓腳本失敗"


def test_docs_resident_explains_whitelist_and_trust_scope():
    text = (REPO / "docs" / "resident.md").read_text(encoding="utf-8")
    for needle in ("rclone-worker.conf", "signing.key", "llm-<provider>.key",
                   "白名單", "同一個 profile 就是同一個信任範圍", "auth.json"):
        assert needle in text
