"""一發指令跑完需要真 Drive、真 git-annex、真 pin-test repo 的測試。

`tests/integration/` 預設跑；`--include-e2e` 加跑 `tests/e2e/`。

為什麼有這一支（`docs/testing.md` 有完整說明）：
- 整合測試要設定齊（ids.env、rclone 設定檔、deploy key、known_hosts）與
  四個外部程式（git-annex、rclone、git-filter-repo、git）；缺東西時
  `pytest` 只會丟一個 `MissingIntegrationSetting`，看不出來到底缺什麼。
  這支在開跑前把缺項一次講清楚。
- 預設序列化跑完再下一支（不開 xdist）：pin-test repo 是所有線共用的，
  同時跑會互相覆蓋釘選值（`sandbox.pin_repo_name()` 用唯一名稱避開，但
  同一個 repo 上的 push 仍會互相干擾）。
- 收尾比對跑前／跑後的 Drive 前綴與 pin repo 條目，把這一輪的殘留列出來。
- 結束印摘要並回傳正確的 exit code，搬到 GitHub Actions 時 CI 直接看得懂。

秘密一律**只以路徑引用**：這支腳本不讀、不印任何金鑰或 token 的內容，
連 rclone 設定檔都只檢查檔案存不存在。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AISTORAGE_HOME = Path.home() / ".config" / "aistorage"

#: 憑證檔（值一律是路徑；內容不讀）。
IDS_ENV = AISTORAGE_HOME / "ids.env"
RCLONE_CONF = AISTORAGE_HOME / "rclone-committer-test.conf"
PIN_KEY = AISTORAGE_HOME / "pin-test.key"
KNOWN_HOSTS = REPO_ROOT / "config" / "github_known_hosts"
PIN_REPO_URL = "git@github.com:FATESAIKOU/MyAiStorage-pin-test.git"

#: 覆寫用的環境變數（與 `tests/integration/conftest.py` 用同一組）。
ENV_IDS = "AISTORAGE_TEST_IDS"
ENV_RCLONE_CONF = "AISTORAGE_TEST_RCLONE_CONF"
ENV_PIN_KEY = "AISTORAGE_TEST_PIN_KEY"
ENV_KNOWN_HOSTS = "AISTORAGE_TEST_KNOWN_HOSTS"
ENV_PIN_REPO = "AISTORAGE_TEST_PIN_REPO"

#: 外部程式與「取版本字串」的方式。
TOOLS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("git", ("--version",)),
    ("git-annex", ("version",)),
    ("rclone", ("version",)),
    ("git-filter-repo", ("--version",)),
)


# ---------------------------------------------------------------------------
# 設定解析與 preflight
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """整合測試要用的四個路徑與 pin repo URL（沒有任何秘密內容）。"""

    ids_env: Path
    rclone_conf: Path
    pin_key: Path
    known_hosts: Path
    pin_repo_url: str


def resolve_settings(env: Mapping[str, str] | None = None) -> Settings:
    """把四個憑證檔與 pin repo URL 解析出來；值一律是路徑。

    預設指向 `~/.config/aistorage/…`（known_hosts 例外，它是 repo 內的非秘密檔），
    可用與 conftest 相同的環境變數覆寫。
    """
    env = os.environ if env is None else env

    def pick(default: Path, var: str) -> Path:
        override = env.get(var)
        return Path(override) if override else default

    return Settings(
        ids_env=pick(IDS_ENV, ENV_IDS),
        rclone_conf=pick(RCLONE_CONF, ENV_RCLONE_CONF),
        pin_key=pick(PIN_KEY, ENV_PIN_KEY),
        known_hosts=pick(KNOWN_HOSTS, ENV_KNOWN_HOSTS),
        pin_repo_url=env.get(ENV_PIN_REPO) or PIN_REPO_URL,
    )


@dataclass(frozen=True)
class Preflight:
    """preflight 結果：`problems` 一旦非空就不該開跑。"""

    problems: tuple[str, ...]
    test_folder_id: str | None
    tool_versions: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


def read_test_folder_id(ids_env: Path) -> str | None:
    """從 ids.env 讀 `TEST_FOLDER_ID`（Drive 的資料夾 id，不是秘密）。

    回傳 None 代表檔案裡沒有這個變數；呼叫者負責講缺什麼。
    """
    import re

    try:
        text = ids_env.read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(r"^\s*TEST_FOLDER_ID\s*=\s*(\S+)\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def check_credentials(
    settings: Settings, env: Mapping[str, str] | None = None
) -> tuple[list[str], str | None]:
    """檢查四個憑證檔在不在、`TEST_FOLDER_ID` 有沒有設。

    回傳 (問題清單, TEST_FOLDER_ID 或 None)。訊息裡只出現路徑，不出現內容。
    """
    env = os.environ if env is None else env
    problems: list[str] = []
    for path, what in (
        (settings.ids_env, "Drive 測試資料夾 id 的來源檔（要有 TEST_FOLDER_ID）"),
        (settings.rclone_conf, "rclone 設定檔"),
        (settings.pin_key, "pin-test repo 的 deploy key 私鑰"),
        (settings.known_hosts, "GitHub known_hosts"),
    ):
        if not path.is_file():
            problems.append(f"找不到 {what}：{path}")

    folder_id = None
    if settings.ids_env.is_file():
        folder_id = env.get("TEST_FOLDER_ID") or read_test_folder_id(settings.ids_env)
        if not folder_id:
            problems.append(
                f"{settings.ids_env} 裡沒有 TEST_FOLDER_ID"
                "（整合測試的 Drive 前綴建在它底下；不設就用環境變數 TEST_FOLDER_ID 帶入）"
            )
    return problems, folder_id


def check_tools(
    which: Callable[[str], str | None] | None = None,
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None = None,
) -> tuple[list[str], list[str]]:
    """檢查外部程式並取回版本字串（只印版本，不印任何秘密）。

    `which` / `run` 可注入，方便單元測試不用真的去 call 外部程式。
    """
    which = shutil.which if which is None else which
    problems: list[str] = []
    versions: list[str] = []
    for tool, args in TOOLS:
        found = which(tool)
        if not found:
            problems.append(f"找不到 {tool}（整合測試需要它；macOS: brew install {tool}）")
            continue
        if run is None:
            versions.append(f"{tool}: 已安裝（{found}）")
            continue
        try:
            proc = run([tool, *args])  # type: ignore[arg-type]
        except OSError:
            problems.append(f"{tool} 執行失敗（路徑：{found}）")
            continue
        first = (proc.stdout or "").strip().splitlines()
        head = first[0].strip() if first else "（沒有輸出）"
        if proc.returncode != 0:
            problems.append(f"{tool} 執行失敗（`{tool} {' '.join(args)}` → rc={proc.returncode}）")
            continue
        versions.append(f"{tool}: {head}")
    return problems, versions


def preflight(
    settings: Settings | None = None,
    env: Mapping[str, str] | None = None,
    *,
    which=None,
    run=None,
) -> Preflight:
    """開跑前把所有缺項收齊：憑證檔、`TEST_FOLDER_ID`、外部程式。"""
    settings = settings or resolve_settings(env)
    problems, folder_id = check_credentials(settings, env)
    tool_problems, tool_versions = check_tools(which=which, run=run)
    return Preflight(
        problems=tuple(problems + tool_problems),
        test_folder_id=folder_id,
        tool_versions=tuple(tool_versions),
    )


# ---------------------------------------------------------------------------
# 殘留檢查
# ---------------------------------------------------------------------------


def diff_names(before: Sequence[str], after: Sequence[str]) -> list[str]:
    """回傳 `after` 有、`before` 沒有的項目（依排序，值去重）。

    純函式，單元測試直接餵兩份清單。
    """
    seen_before = set(before)
    return sorted({name for name in after if name not in seen_before})


def drive_prefix_names(drive, test_root_id: str) -> list[str]:
    """列出 Drive 測試根資料夾底下的直接子資料夾名（只取名，不取 id）。

    只看直接子層：整合測試的前綴都在這裡，隔離／暫存資料夾是前綴的子孫，
    前綴被刪掉就一起消失。
    """
    names: list[str] = []
    for child in drive.list_children(test_root_id):
        names.append(str(getattr(child, "name", "")))
    return names


def pin_entry_names(settings: Settings, workdir: Path) -> list[str]:
    """用 deploy key 把 pin repo 淺複製到 `workdir`，回傳 `.pin/` 底下的條目名。

    只讀不寫；clone 用 `GIT_SSH_COMMAND` 隔離個人身分（和 `GitPinStore` 一致）。
    """
    workdir.mkdir(parents=True, exist_ok=True)
    dest = workdir / "pin"
    env = dict(os.environ)
    env["GIT_SSH_COMMAND"] = (
        f"ssh -F /dev/null -i {settings.pin_key} -o IdentitiesOnly=yes "
        f"-o StrictHostKeyChecking=yes -o UserKnownHostsFile={settings.known_hosts} "
        "-o IdentityAgent=none"
    )
    subprocess.run(
        ["git", "clone", "--depth", "1", "--branch", "main", settings.pin_repo_url, str(dest)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    pin_dir = dest / ".pin"
    if not pin_dir.is_dir():
        return []
    return sorted(p.stem for p in pin_dir.iterdir() if p.is_file())


def snapshot(settings: Settings, test_folder_id: str, workdir: Path) -> dict[str, list[str]]:
    """跑前／跑後各拍一張快照：Drive 前綴名 + pin repo 條目名。"""
    drive_prefix: list[str] = []
    try:
        from aistorage.drive.auth import RcloneConfToken
        from aistorage.drive.http import HttpDriveClient

        drive = HttpDriveClient(RcloneConfToken(settings.rclone_conf, remote="gdrive"))
        drive_prefix = drive_prefix_names(drive, test_folder_id)
    except Exception:  # noqa: BLE001 - 殘留檢查是加分項：Drive 掛了不該讓整場結果失效
        drive_prefix = []
    pin_entries = pin_entry_names(settings, workdir)
    return {"drive": drive_prefix, "pin": pin_entries}


# ---------------------------------------------------------------------------
# 跑測試
# ---------------------------------------------------------------------------


@dataclass
class PhaseResult:
    name: str
    returncode: int
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    duration_s: float = 0.0

    @property
    def counted(self) -> int:
        return self.passed + self.failed + self.skipped


@dataclass
class Summary:
    phases: list[PhaseResult] = field(default_factory=list)
    leftovers: dict[str, list[str]] = field(default_factory=dict)
    leftover_checked: bool = False
    duration_s: float = 0.0

    @property
    def passed(self) -> int:
        return sum(p.passed for p in self.phases)

    @property
    def failed(self) -> int:
        return sum(p.failed for p in self.phases)

    @property
    def skipped(self) -> int:
        return sum(p.skipped for p in self.phases)

    def exit_code(self, *, strict_leftovers: bool = False) -> int:
        """任一階段失敗 → 1；嚴格殘留模式下有殘留也 → 1。"""
        if any(p.returncode != 0 for p in self.phases):
            return 1
        if strict_leftovers and any(self.leftovers.values()):
            return 1
        return 0


def parse_junit(path: Path) -> tuple[int, int, int]:
    """從 pytest 的 junitxml 讀 (通過, 失敗, 略過)。

    讀不到就回 (0, 0, 0)——計數只是給人看的，判成败靠 pytest 自己的 exit code。
    """
    if not path.is_file():
        return (0, 0, 0)
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return (0, 0, 0)
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    passed = failed = skipped = 0
    for suite in suites:
        failed += int(suite.get("failures") or 0) + int(suite.get("errors") or 0)
        skipped += int(suite.get("skipped") or 0)
        passed += int(suite.get("tests") or 0) - failed - skipped
    return (max(passed, 0), failed, skipped)


def pytest_argv(
    phase: str, *, only: str | None, junit: Path
) -> list[str]:
    """組 pytest 指令列。

    - `-p no:cacheprovider` 不留 `.pytest_cache`（CI 每次都是乾淨工作區）。
    - `--tb=short` 且**永遠不開** `-l` / rich traceback：N8 說本機區域變數
      可能含秘密或未過濾路徑，不能印進 Actions log。
    """
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "tests/integration" if phase == "integration" else "tests/e2e",
        "-m",
        phase,
        "-q",
        "--tb=short",
        "-p",
        "no:cacheprovider",
        f"--junitxml={junit}",
    ]
    if only:
        argv += ["-k", only]
    return argv


def run_phase(
    phase: str, *, only: str | None, junit: Path, env: Mapping[str, str] | None = None
) -> PhaseResult:
    """跑一個階段的 pytest；回傳結果（含從 junitxml 讀來的計數）。"""
    argv = pytest_argv(phase, only=only, junit=junit)
    printable = " ".join(argv[2:])
    print(f"\n=== {phase} ===\n[run] {printable}", flush=True)
    started = time.monotonic()
    proc = subprocess.run(argv, cwd=str(REPO_ROOT), env=dict(env or os.environ), check=False)
    duration = time.monotonic() - started
    passed, failed, skipped = parse_junit(junit)
    return PhaseResult(
        name=phase,
        returncode=proc.returncode,
        passed=passed,
        failed=failed,
        skipped=skipped,
        duration_s=duration,
    )


# ---------------------------------------------------------------------------
# 輸出
# ---------------------------------------------------------------------------


def format_issues(problems: Sequence[str]) -> str:
    """把 preflight 的缺項講成一句話可以追的樣子（每項一行 + 怎麼修）。"""
    lines = ["preflight 沒過，以下東西不能用："]
    lines += [f"  - {p}" for p in problems]
    lines.append("  設定位置與取得方式見 docs/testing.md；覆寫用環境變數：")
    lines.append(
        f"    {ENV_IDS} / {ENV_RCLONE_CONF} / {ENV_PIN_KEY} / {ENV_KNOWN_HOSTS} / {ENV_PIN_REPO}"
    )
    return "\n".join(lines)


def format_summary(summary: Summary, *, strict_leftovers: bool) -> str:
    """結束摘要：各階段計數、殘留、總時間。CI log 讀這段就夠。"""
    lines = ["=== 摘要 ==="]
    for phase in summary.phases:
        lines.append(
            f"  {phase.name}: 過 {phase.passed}、失敗 {phase.failed}、略過 {phase.skipped}"
            f"（{phase.duration_s:.1f}s，exit={phase.returncode}）"
        )
    if not summary.phases:
        lines.append("  （沒有跑任何階段）")
    if summary.leftover_checked:
        leftovers = [f"{k}: {v}" for k, v in summary.leftovers.items() if v]
        if leftovers:
            lines.append("  殘留（這一輪留下來的東西，沒自動清）：")
            lines += [f"    - {item}" for item in leftovers]
        else:
            lines.append("  殘留：無（Drive 前綴與 pin repo 都回到跑前的樣子）")
    else:
        lines.append("  殘留：沒檢查（拿不到 Drive 或 pin repo）")
    lines.append(
        f"  合計：過 {summary.passed}、失敗 {summary.failed}、略過 {summary.skipped}"
        f"，總時間 {summary.duration_s:.1f}s"
    )
    lines.append(f"  結果：{'失敗' if summary.exit_code(strict_leftovers=strict_leftovers) else '全部通過'}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """參數解析。`--only` 交給 pytest 的 `-k`（比對測試節點 id，含檔名）。"""
    parser = argparse.ArgumentParser(
        prog="run-integration",
        description="跑需要真 Drive／真 git-annex／真 pin-test repo 的測試",
    )
    parser.add_argument(
        "--only",
        metavar="PATTERN",
        default=None,
        help="只跑 id 含 PATTERN 的測試（等同 pytest -k；例：--only three_rounds）",
    )
    parser.add_argument(
        "--include-e2e",
        action="store_true",
        help="整合測試之後再跑 tests/e2e（需要 e2e 環境已 setup 好）",
    )
    parser.add_argument(
        "--skip-leftovers",
        action="store_true",
        help="不跑收尾的殘留檢查（Drive 或 pin repo 連不上時用）",
    )
    parser.add_argument(
        "--strict-leftovers",
        action="store_true",
        help="有殘留時 exit code 也算 1（CI 用；預設只列出來）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    settings = resolve_settings()
    result = preflight(settings)
    print("=== preflight ===")
    for line in result.tool_versions:
        print(f"  {line}")
    if not result.ok:
        print(format_issues(result.problems), flush=True)
        return 2

    phases = ["integration"] + (["e2e"] if args.include_e2e else [])
    summary = Summary()
    started = time.monotonic()
    test_folder_id = str(result.test_folder_id)
    with tempfile.TemporaryDirectory(prefix="run-integration-") as tmp_name:
        tmp = Path(tmp_name)
        before = (
            snapshot(settings, test_folder_id, tmp / "before")
            if not args.skip_leftovers
            else None
        )
        try:
            for phase in phases:
                summary.phases.append(
                    run_phase(phase, only=args.only, junit=tmp / f"{phase}.xml")
                )
        finally:
            summary.duration_s = time.monotonic() - started
            if before is not None:
                after = snapshot(settings, test_folder_id, tmp / "after")
                summary.leftovers = {k: diff_names(before[k], after[k]) for k in before}
                summary.leftover_checked = True

    print()
    print(format_summary(summary, strict_leftovers=args.strict_leftovers), flush=True)
    return summary.exit_code(strict_leftovers=args.strict_leftovers)


if __name__ == "__main__":
    raise SystemExit(main())
