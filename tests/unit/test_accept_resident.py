"""Independent Acceptance Tests for Resident Container (tasks 5.1).

Adheres strictly to:
- docs/impl/group5-7-modules.md §1
- docs/resident.md
- Whitelist enforcement (reject non-whitelisted files on startup)
- Only mount the LLM key of the provider specified by --model
- Reject when signing.key is missing
- Reject when file permissions are too loose (> 0600)
- Verify --print-plan security properties: user (non-root), home=/work, docker_sock=false, cap_add=[]
- Never mount or allow Claude / Anthropic credentials
- LLM apiKey referenced via {file:/secrets/...} instead of plaintext
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
RUN_SH = REPO_ROOT / "resident" / "run.sh"
ENTRYPOINT_SH = REPO_ROOT / "resident" / "image" / "entrypoint.sh"
OPENCODE_BASE_JSON = REPO_ROOT / "resident" / "opencode" / "opencode.base.json"


def _setup_profile_dir(
    root: Path,
    profile: str = "mac-opencode",
    *,
    files: dict[str, str] | None = None,
    mode: int = 0o600,
) -> tuple[Path, Path, dict[str, str]]:
    """Helper to setup secrets_dir and work_dir with given files and permissions."""
    secrets_dir = root / "resident" / profile
    secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir, 0o700)

    work_dir = root / "work"
    work_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(work_dir, 0o700)

    if files is None:
        files = {
            "rclone-worker.conf": "dummy_rclone_conf",
            "sa-reader.json": "{}",
            "signing.key": "32_bytes_dummy_signing_key_ed25519",
            "llm-openai.key": "sk-dummy-openai-key",
            "reader.json": "{}",
        }

    for name, content in files.items():
        p = secrets_dir / name
        p.write_text(content, encoding="utf-8")
        os.chmod(p, mode)

    env = os.environ.copy()
    env["AISTORAGE_RESIDENT_ROOT"] = str(root / "resident")
    env["AISTORAGE_WORK_ROOT"] = str(work_dir)
    return secrets_dir, work_dir, env


def test_resident_rejects_unwhitelisted_files(tmp_path: Path):
    """驗證白名單外的檔名拒絕啟動 (docs/resident.md §3)。"""
    secrets_dir, work_dir, env = _setup_profile_dir(tmp_path)

    # 放入非白名單檔案
    bad_file = secrets_dir / "unauthorized_secret.env"
    bad_file.write_text("SOME_SECRET=123", encoding="utf-8")
    os.chmod(bad_file, 0o600)

    res = subprocess.run(
        [
            str(RUN_SH),
            "test-worker-1",
            "--profile",
            "mac-opencode",
            "--model",
            "openai/gpt-4o",
            "--print-plan",
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert res.returncode != 0
    assert "拒絕啟動" in res.stderr
    assert "不在白名單的檔案" in res.stderr
    assert "unauthorized_secret.env" in res.stderr


def test_resident_mounts_only_specified_provider_key(tmp_path: Path):
    """驗證只掛 --model 指定的 provider 金鑰，最小權限 (docs/resident.md §3)。"""
    files = {
        "rclone-worker.conf": "dummy_rclone_conf",
        "sa-reader.json": "{}",
        "signing.key": "dummy_key",
        "llm-openai.key": "sk-openai",
        "llm-ollama.key": "sk-ollama",
    }
    secrets_dir, work_dir, env = _setup_profile_dir(tmp_path, files=files)

    # 1. 執行 --model openai/gpt-4o -> 應只掛載 llm-openai.key
    res_openai = subprocess.run(
        [
            str(RUN_SH),
            "test-worker-openai",
            "--profile",
            "mac-opencode",
            "--model",
            "openai/gpt-4o",
            "--print-plan",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_openai.returncode == 0
    plan_openai = json.loads(res_openai.stdout)
    assert plan_openai["provider"] == "openai"
    assert "llm-openai.key" in plan_openai["secrets_mounted"]
    assert "llm-ollama.key" not in plan_openai["secrets_mounted"]

    # 2. 執行 --model ollama/llama3 -> 應只掛載 llm-ollama.key
    res_ollama = subprocess.run(
        [
            str(RUN_SH),
            "test-worker-ollama",
            "--profile",
            "mac-opencode",
            "--model",
            "ollama/llama3",
            "--print-plan",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_ollama.returncode == 0
    plan_ollama = json.loads(res_ollama.stdout)
    assert plan_ollama["provider"] == "ollama"
    assert "llm-ollama.key" in plan_ollama["secrets_mounted"]
    assert "llm-openai.key" not in plan_ollama["secrets_mounted"]


def test_resident_rejects_missing_signing_key(tmp_path: Path):
    """驗證缺簽章金鑰拒絕啟動 (docs/impl/group5-7-modules.md §1.2)。"""
    files = {
        "rclone-worker.conf": "dummy_rclone_conf",
        "llm-openai.key": "sk-openai",
        # signing.key 刻意缺省
    }
    secrets_dir, work_dir, env = _setup_profile_dir(tmp_path, files=files)

    res = subprocess.run(
        [
            str(RUN_SH),
            "test-worker-nosign",
            "--profile",
            "mac-opencode",
            "--model",
            "openai/gpt-4o",
            "--print-plan",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode != 0
    assert "缺少必要檔案" in res.stderr
    assert "signing.key" in res.stderr


def test_resident_rejects_loose_permissions(tmp_path: Path):
    """驗證秘密檔權限過寬（> 0600）拒絕啟動 (docs/resident.md §3)。"""
    secrets_dir, work_dir, env = _setup_profile_dir(tmp_path)

    # 將其中一個檔案設為群組/他人可讀（0644）
    os.chmod(secrets_dir / "signing.key", 0o644)

    # 預設模式：嚴格檢查權限 -> 失敗
    res = subprocess.run(
        [
            str(RUN_SH),
            "test-worker-loose",
            "--profile",
            "mac-opencode",
            "--model",
            "openai/gpt-4o",
            "--print-plan",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode != 0
    assert "拒絕掛載權限過寬的秘密檔" in res.stderr
    assert "chmod 600" in res.stderr

    # 設定 AISTORAGE_RESIDENT_ALLOW_LOOSE_PERMS=1 允許寬鬆權限
    env_loose = env.copy()
    env_loose["AISTORAGE_RESIDENT_ALLOW_LOOSE_PERMS"] = "1"
    res_allow = subprocess.run(
        [
            str(RUN_SH),
            "test-worker-loose",
            "--profile",
            "mac-opencode",
            "--model",
            "openai/gpt-4o",
            "--print-plan",
        ],
        capture_output=True,
        text=True,
        env=env_loose,
    )
    assert res_allow.returncode == 0


def test_resident_print_plan_security_constraints(tmp_path: Path):
    """驗證 --print-plan 的 user、home、docker_sock、cap_add 等安全邊界 (docs/resident.md §4)。"""
    secrets_dir, work_dir, env = _setup_profile_dir(tmp_path)

    res = subprocess.run(
        [
            str(RUN_SH),
            "test-worker-sec",
            "--profile",
            "mac-opencode",
            "--model",
            "openai/gpt-4o",
            "--print-plan",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res.returncode == 0
    plan = json.loads(res.stdout)

    # 1. 執行者 user 必須是非 root（以本機 uid:gid 執行）
    expected_user = f"{os.getuid()}:{os.getgid()}"
    assert plan["user"] == expected_user
    assert plan["user"] != "0:0"

    # 2. HOME 設定為 /work
    assert plan["home"] == "/work"

    # 3. 不掛 docker.sock
    assert plan["docker_sock"] is False

    # 4. cap_add 為空陣列（不加任何 Linux capabilities）
    assert plan["cap_add"] == []

    # 5. 非 privileged
    assert plan["privileged"] is False

    # 6. 工作目錄隔離在各自容器名下
    assert plan["work_dir"] == str(work_dir / "test-worker-sec")


def test_resident_no_claude_credentials(tmp_path: Path):
    """驗證不掛任何 Claude 憑證，並主動拒絕 Claude 環境變數與設定 (docs/resident.md §3)。"""
    secrets_dir, work_dir, env = _setup_profile_dir(tmp_path)

    # 1. run.sh 白名單拒絕任何 Claude 相關金鑰檔
    claude_key = secrets_dir / "anthropic.key"
    claude_key.write_text("sk-ant-secret", encoding="utf-8")
    os.chmod(claude_key, 0o600)

    res_run = subprocess.run(
        [
            str(RUN_SH),
            "test-worker-claude",
            "--profile",
            "mac-opencode",
            "--model",
            "openai/gpt-4o",
            "--print-plan",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert res_run.returncode != 0
    assert "anthropic.key" in res_run.stderr
    claude_key.unlink()

    # 2. entrypoint.sh 執行階段主動檢測並拒絕 ANTHROPIC_API_KEY
    env_claude = env.copy()
    env_claude["ANTHROPIC_API_KEY"] = "sk-ant-test-token"
    env_claude["AISTORAGE_MODEL"] = "openai/gpt-4o"
    res_entry = subprocess.run(
        ["bash", str(ENTRYPOINT_SH)],
        capture_output=True,
        text=True,
        env=env_claude,
    )
    assert res_entry.returncode != 0
    assert "拒絕啟動：環境變數 ANTHROPIC_API_KEY 存在" in res_entry.stderr

    # 3. entrypoint.sh 檢測 CLAUDE_API_KEY
    env_claude2 = env.copy()
    env_claude2["CLAUDE_API_KEY"] = "sk-claude-test"
    env_claude2["AISTORAGE_MODEL"] = "openai/gpt-4o"
    res_entry2 = subprocess.run(
        ["bash", str(ENTRYPOINT_SH)],
        capture_output=True,
        text=True,
        env=env_claude2,
    )
    assert res_entry2.returncode != 0
    assert "拒絕啟動：環境變數 CLAUDE_API_KEY 存在" in res_entry2.stderr


def test_resident_llm_key_file_reference_in_config():
    """驗證 LLM 金鑰以 {file:} 檔案引用，防止 opencode 寫出明文 auth.json (docs/resident.md §3)。"""
    # 透過 entrypoint.sh 的 render-config 模式產出設定
    res = subprocess.run(
        [
            "bash",
            str(ENTRYPOINT_SH),
            "render-config",
            "openai",
            "openai/gpt-4o",
            str(OPENCODE_BASE_JSON),
        ],
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    config = json.loads(res.stdout)

    assert config["model"] == "openai/gpt-4o"
    openai_cfg = config["provider"]["openai"]["options"]
    api_key = openai_cfg["apiKey"]

    # 驗證格式為 {file:/secrets/llm-<provider>.key}
    assert api_key == "{file:/secrets/llm-openai.key}"
    assert api_key.startswith("{file:")
    assert api_key.endswith("}")
    assert "/secrets/llm-openai.key" in api_key

    # 驗證沒有明文金鑰
    assert "sk-" not in json.dumps(config)
