# 正式部署手冊（AiStorage 期 1 上線）

從「測試資源都已驗證、正式身分已佈置」走到「正式上線並可日常運作」的逐步程序。
**本檔只寫步驟，不代替執行**；每一步都要照「誰做 → 指令 → 怎麼驗證 → 失敗怎麼退」做完再往下。

- 適用對象：使用者本人（管理）與 PM（在本機執行 CLI）。
- 依據：`docs/resources.md`（實體資源清單）、`docs/decision-log.md`、`docs/adr/`、
  `docs/identity-setup.md`（簽章金鑰）、`docs/runbooks/{erase,rollback,health,recovery}.md`（上線後的維運）。
- 資料夾與 repo 的 id 全部要填回 `docs/resources.md`（只記 id，不記秘密）。

## 角色標記

| 標記 | 誰做 | 在哪裡做 |
|---|---|---|
| 👤 使用者 | 你本人 | 網頁（Google Drive、GitHub 設定頁） |
| 🛠 PM | 助理（Claude） | Mac 上的 CLI（本檔的 `uv run` 指令），工作目錄＝專案 repo |

> ⚠ 全程遵守兩條：`git`／`git-annex` 的工作目錄必須在**暫存目錄**（`safety.assert_safe_workdir` 會擋掉專案 repo）；任何一步失敗就停在該步，**不要**跳過或「先跑看看」。

## 事前檢查清單（全部 OK 才開始）

1. `docs/resources.md` 的「期 1 正式身分」表已建立：committer client（project 1）、
   worker client（project 2）、`spike-reader@…` SA、`FATESAIKOU/MyAiStorage-pin`、
   deploy key 私鑰 `~/.config/aistorage/pin-deploy-key`。
2. 正式 pin repo **還沒有**任何 `.pin/*.json`（還沒 `init-pin`）。
3. `FATESAIKOU/MyAiStorage` 的 Actions **還沒有**啟用 `committer` workflow。
4. Mac 上：`uv --version`、`git --version`、`git-annex version`、`rclone version`、`gh auth status` 都可用。
5. `~/.config/aistorage/` 權限 700、其中 conf／key 檔 600：
   ```bash
   ls -ld ~/.config/aistorage && ls -l ~/.config/aistorage | sed -n '1,12p'
   ```
6. 測試前綴（`it-*`／`agora-*`）與正式前綴**名字不同**，後面所有步驟都不會碰到測試資料。

---

## 步驟 1｜建立正式資料夾（committer 身分）

**誰做**：🛠 PM（用 committer 的 rclone conf 走 Drive API，不用網頁）

建立一個共同根與其下三個資料夾。共同根之後會設成 `root_folder_id`（步驟 6），
把提交流程與 git-annex 的 blast radius 限制在這個根底下。

> ADR 0009：期 1 只有 Agora 有寫入閘門與讀取視圖，所以不需要 Foundry 的前綴、
> 隔離區與讀取視圖。Foundry 改成 Drive 共享資料夾＋GitHub，不需要 git-annex
> repo（見 `docs/backlog.md` 的「新 Foundry」）。

```
aistorage/                        ← 共同根（root_folder_id）
├── agora/                        ← Agora 真本前綴（GITMANIFEST／GITBUNDLE／annex 物件）
├── agora-quarantine/             ← 隔離區（與前綴同一層；提交流程的清掃要求「佈局是平的」）
└── readview/                     ← Agora 讀取視圖（SA 讀取）
```

> 收件匣**不在這個根底下**：worker 的 `drive.file` 憑證只看得見「自己建立的檔案」，
> 所以收件匣由 worker 自己的憑證建立（步驟 9），提交流程依 id 讀它。依據是實測：
> `docs/spike/evidence/1.4-drive-file-isolation.md` 1.4i「profile 自己的 client 可以建
> 收件匣；committer 依 id 讀取成功」。

```bash
# 建立資料夾並印出 id（只印 id 與名稱，不印任何憑證）
cd /path/to/MyAiStorage
uv run python - <<'PY'
from pathlib import Path
from aistorage.drive import HttpDriveClient, RcloneConfToken
from aistorage.drive.model import GOOGLE_FOLDER_MIME

conf = Path.home() / ".config/aistorage/rclone-committer.conf"
drive = HttpDriveClient(RcloneConfToken(conf, remote="gdrive"))
# 帳號根資料夾 id：從測試資料夾的 parent 反推（Drive client 沒有 get root 的方法）
test_id = ""
for line in (Path.home() / ".config/aistorage/ids.env").read_text().splitlines():
    if line.startswith("TEST_FOLDER_ID="):
        test_id = line.split("=", 1)[1].strip()
account_root = drive.get(test_id).parents[0]
print("帳號根 =", account_root)

root = drive.create(account_root, "aistorage", b"", mime_type=GOOGLE_FOLDER_MIME)
print("aistorage/", root.id)
for name in ("agora", "agora-quarantine", "readview"):
    f = drive.create(root.id, name, b"", mime_type=GOOGLE_FOLDER_MIME)
    print(f"{name:20s} {f.id}")
PY
```

**怎麼驗證**

```bash
uv run python - <<'PY'
from pathlib import Path
from aistorage.drive import HttpDriveClient, RcloneConfToken
drive = HttpDriveClient(RcloneConfToken(Path.home()/".config/aistorage/rclone-committer.conf",
                                        remote="gdrive"))
for f in drive.list_children("<aistorage/ id>"):          # 換成上一步印出的 aistorage/ id
    print(f.name, f.id, "folder" if f.is_folder else "file")
PY
```
三個子資料夾都在、而且沒有任何檔案（乾淨）。

**失敗怎麼退**：逐一 `drive.delete_permanently(<id>)`（Drive API 的刪除是永久的，不進垃圾桶），
再刪 `aistorage/` 本身。已填進 `docs/resources.md` 的 id 要一併刪掉那一列。

**要填回 `docs/resources.md`**：`aistorage/` 與三個子資料夾的 id。

---

## 步驟 2｜讀取權限（由步驟 5 的指令與這一步處理）

**誰做**：🛠 PM

讀取端以 **service account** 讀，Drive ACL 必須有它。**要分享的是兩個資料夾，
都只給 `reader`（唯讀）**：

| 資料夾 | 為什麼需要 | 誰分享 |
|---|---|---|
| `readview/` | 讀 manifest、index 與各份 reading | 步驟 5 的 `init-readview --confirm` |
| `agora/`（真本前綴） | **`agora checkout` 依快照的 annex key 取原始紀錄本體** | 這一步 |

分享資料夾而不是個別檔案是必要的：讀取端要依 id 讀 manifest、index 與各份
reading（做法與 1.5 驗證過的 `files.permissions.create` 相同）。

**為什麼真本前綴也要給唯讀**：`agora checkout` 產出的起點包要放**原始紀錄原封
不動**（ADR 0010：新 session 送給模型的開頭要與原 session 位元組相同）。閱讀版
做不到（它把工具呼叫的輸入輸出壓成摘要），而讀取視圖**刻意不發佈** raw 的位元組
——那等於把真本的位元組複製一份到衍生物裡。所以路徑是「讀取介面只給位址
（annex key）→ 唯讀身分自己去真本前綴取 → **用 key 內嵌的 sha256 與 size
驗證**」。key 是內容定址的位址，所以「塞一份同名假檔」過不了那一步；取不到或
對不上就明確拒絕、不產出起點包。

**所以：唯讀就夠了，不要給 writer／fileOrganizer。** 讀取身分不該能改真本。

### 讀取邊界（M4，使用者已接受）：原始紀錄是完整的

上面「真本前綴也要給唯讀」這件事有一個直接後果，寫進來是為了不要有人
（人或 AI）事後覺得意外：**讀取端讀得到的是完整原始紀錄，不是閱讀版**。所以

- **模型的思考**（reasoning／thinking）在裡面；
- **完整的工具輸出**在裡面——某一次 `cat .env`、`env`、`printenv` 印出來的
  **值**會原樣留在那裡。原始紀錄是「當時真的送給模型的東西」，閱讀版會截斷或
  省略，它不會。

**因此住民不要把秘密印進工具輸出。** 一旦印出去，它就跟著原始紀錄進 Agora，
被同一個信任範圍內的住民讀到，而且**抹不掉**（期 1 沒有改寫；抹除只有你本人
能做）。要確認某個東西設定好了，打印長度或前綴，不要印本體；已經誤印就回報，
不要自行嘗試修改 Agora。

這與權限無關：`agora-quarantine/` 不分享、Drive ACL 擋的是**寫入**；讀取端
本來就設計成讀得到完整紀錄。要更緊的邊界就是不要把秘密放進會被工具輸出印到的
地方（不把憑證檔放在會被 `cat` 的路徑、prompt 裡不要帶 token）。

詳細的住民側說明見 `docs/resident.md` 的「讀取邊界」。

```bash
export AISTORAGE_RCLONE_CONF="$HOME/.config/aistorage/rclone-committer.conf"
# 把步驟 1 的 agora 前綴 id 唯讀分享給讀取用 SA（冪等：已分享就跳過）
uv run python - <<'PY'
import json, os
from pathlib import Path
from aistorage.drive import HttpDriveClient, RcloneConfToken

drive = HttpDriveClient(RcloneConfToken(os.environ["AISTORAGE_RCLONE_CONF"]))
sa = "spike-reader@aistorage-spike-1-260926.iam.gserviceaccount.com"
folder = "<步驟 1 的 agora 前綴 id>"
listing = f"https://www.googleapis.com/drive/v3/files/{folder}/permissions?fields=permissions(id,role,type,emailAddress)"
_, _, body = drive._request(listing, method="GET")
if any(p.get("emailAddress") == sa for p in json.loads(body).get("permissions", [])):
    print("已經是唯讀共用，跳過")
else:
    create = f"https://www.googleapis.com/drive/v3/files/{folder}/permissions?fields=id,role,type,emailAddress"
    payload = json.dumps({"type": "user", "role": "reader",
                          "emailAddress": sa}).encode()
    drive._request(create, method="POST",
                   headers={"Content-Type": "application/json"},
                   data=payload, is_write=True)
    print(f"已用 role=reader 共用 {folder} 給 {sa}")
PY
```

**怎麼驗證**（SA 金鑰實際讀一次，🛠 PM）

```bash
uv run python - <<'PY'
from pathlib import Path
from aistorage.drive import HttpDriveClient
from aistorage.drive.sa_auth import ServiceAccountToken
key = Path.home() / ".config/aistorage-spike/sa-reader.json"   # 只以路徑引用
drive = HttpDriveClient(ServiceAccountToken(key))
for f in drive.list_children("<readview 資料夾 id>"):          # 換成步驟 1 的 id
    print(f.name, f.id)
# 真本前綴：應該讀得到（annex 物件是 SHA256E-… 開頭的檔名）
names = [f.name for f in drive.list_children("<步驟 1 的 agora 前綴 id>")]
print("annex 物件數:", sum(1 for n in names if n.startswith("SHA256")))
PY
```
讀取視圖看得到 manifest（此時只有 manifest.json），真本前綴看得到 annex 物件，
就代表兩邊的唯讀權限都到位。

**失敗怎麼退**：SA 金鑰不動；把該協作者從對應資料夾移除即可（Drive 網頁：
共用 → 移除）。已經建立的 manifest 要刪掉才會回到「未初始化」，見步驟 5 的退法。

## 步驟 3｜初始化 Agora 的 git-annex 遠端（產生第一個 manifest）

**誰做**：🛠 PM（暫存目錄內；**不在專案 repo 裡跑 git**）

`init-pin` 要讀前綴裡的主 manifest，所以必須先把 annex special remote 建好並推一個空
真本上去。這是 1.2／整合測試（`tests/integration/_harness.py: build_annex_repo`）做過的
同一套動作，只是改成正式前綴。

```bash
cd "$(mktemp -d)"                       # 暫存目錄；git-annex 的工作目錄防呆靠這裡
export RCLONE_CONFIG="$HOME/.config/aistorage/rclone-committer.conf"
export AGORA_PREFIX="aistorage/agora"   # rcloneprefix 只能是資料夾「名稱路徑」

uv run --project /path/to/MyAiStorage python - <<'PYEOF2'
import os
import subprocess
from pathlib import Path

w = Path.cwd()


def g(*args: str) -> str:
    p = subprocess.run(["git", "-C", str(w), *args],
                       capture_output=True, text=True, check=True)
    return p.stdout


g("init", "-b", "main", "-q")
g("config", "user.name", "AiStorage Admin")
g("config", "user.email", "admin@aistorage.local")
Path("_committer").mkdir(exist_ok=True)
Path("_committer/schema_version").write_text("agora/v1\n")
Path("README.md").write_text("AiStorage Agora\n")
g("add", ".")
g("commit", "-qm", "init: agora seed")
g("annex", "init", "aistorage")
g("annex", "initremote", "drive", "type=rclone", "encryption=none",
  "rcloneremotename=gdrive", f"rcloneprefix={os.environ['AGORA_PREFIX']}",
  "autoenable=true", "--with-url")
g("annex", "config", "--set", "annex.largefiles", "include=sessions/*/*/raw")
g("config", "annex.max-git-bundles", "10")
g("annex", "copy", "--to", "drive")
g("push", "drive", "main", "git-annex")
info = g("annex", "info", "drive", "--fast")
uuid = [l.split(":", 1)[1].strip() for l in info.splitlines() if l.startswith("uuid:")][0]
print("ANNEX_UUID=" + uuid)
print("ANNEX_URL=annex::" + uuid + "?encryption=none&type=rclone"
      "&rcloneremotename=gdrive&rcloneprefix=" + os.environ["AGORA_PREFIX"])
PYEOF2
```

**怎麼驗證**

```bash
# a) Drive 前綴裡應該已經有 manifest 與至少一個 bundle
uv run python - <<'PY'
from pathlib import Path
from aistorage.drive import HttpDriveClient, RcloneConfToken
drive = HttpDriveClient(RcloneConfToken(Path.home()/".config/aistorage/rclone-committer.conf",
                                        remote="gdrive"))
for f in drive.list_children("<agora 前綴 id>"):
    print(f.name, f.size)
PY
```
應該看到 `GITMANIFEST--<uuid>` 與 `GITBUNDLE-s<size>--<uuid>-<sha>`。

**怎麼驗證（b）**：`git annex info drive --fast` 印出的 uuid 要跟上面一致；`git ls-remote drive`
有 `refs/heads/main` 與 `refs/heads/git-annex`。

**失敗怎麼退**：`git push` 失敗多半是 `rcloneprefix` 寫錯（它只能是名稱路徑）。
把該前綴底下的檔案依 file id 永久刪除，重跑本步驟（用同一個暫存目錄即可）。

---

## 步驟 4｜`init-pin`（建立正式釘選值）

**誰做**：🛠 PM（Mac 上、管理者身分；**只跑一次**）

```bash
cd /path/to/MyAiStorage
export AISTORAGE_RCLONE_CONF="$HOME/.config/aistorage/rclone-committer.conf"
export AISTORAGE_PIN_KEY="$HOME/.config/aistorage/pin-deploy-key"
export AISTORAGE_PIN_KNOWN_HOSTS="config/github_known_hosts"

# 先填好 config/committer.json 的 repo_uuid / prefix_folder_id / quarantine_folder_id /
# readview_folder_id（步驟 5 之後再填 readview_manifest_file_id）

# (a) 乾跑：只會印出計畫，不寫 pin repo
uv run python -m aistorage.committer init-pin --config config/committer.json --dry-run

# (b) 確認計畫合理後才寫入
uv run python -m aistorage.committer init-pin --config config/committer.json \
  --confirm --i-am-admin
```

**怎麼驗證**

```bash
gh api repos/FATESAIKOU/MyAiStorage-pin/contents/.pin --jq '.[].name'
# 應該看到 agora.json 與 agora.keys（不是 .pending）
uv run python - <<'PY'
# 內容只有 id 與雜湊，可以印；不要印 annex 物件內容
import json, subprocess
out = subprocess.run(["gh", "api", "repos/FATESAIKOU/MyAiStorage-pin/contents/.pin/agora.json",
                      "--jq", ".content"], capture_output=True, text=True, check=True)
import base64
print(json.dumps(json.loads(base64.b64decode(out.stdout)), indent=2, ensure_ascii=False)[:600])
PY
```

**怎麼驗證（b）**：`manifest_sha256` 要等於前綴裡主 manifest 內容的 sha256；
`refs` 兩筆（main／git-annex）與 `git ls-remote drive` 相同；`run_id` 是 `init-pin`。

**失敗怎麼退**：pin repo 是兩階段的，寫到一半會留 `.pending`。
`gh api` 刪掉 `.pin/agora.pending.json` 與 `.pin/agora.pending.keys` 即可（此時還沒有正式值，
重跑 `init-pin --confirm` 就行）。**已經寫入正式值之後要改，必須走復原手冊**
（`docs/runbooks/recovery.md`），不要手改 pin。

---

## 步驟 5｜初始化讀取視圖 manifest（generation = 0）

**誰做**：🛠 PM（用提交流程的身分；一次性的管理操作）

讀取介面以 **Drive file id** 定位 manifest（D5／PM 決定 10），所以要先有一個檔案、
把 id 記下來。之後每一輪由 publisher 以 `update_content` 原地更新（id 不變）。
`admin init-readview` 一次做完三件事：建立 generation 0 的空 manifest、把資料夾
分享給讀取用 SA、印出 manifest file id（非秘密）。**已存在就拒絕覆寫**——
manifest 是讀取端的信任錨點，覆寫它等於改掉信任根。

```bash
cd /path/to/MyAiStorage
export AISTORAGE_RCLONE_CONF="$HOME/.config/aistorage/rclone-committer.conf"

# (a) 先看計畫（不寫入、不分享）
uv run python -m aistorage.admin init-readview \
  --folder-id <步驟 1 的 readview id> \
  --sa-email spike-reader@aistorage-spike-1-260926.iam.gserviceaccount.com \
  --dry-run

# (b) 確認計畫（blocked=false、folder_children 是空的）後才建立
uv run python -m aistorage.admin init-readview \
  --folder-id <步驟 1 的 readview id> \
  --sa-email spike-reader@aistorage-spike-1-260926.iam.gserviceaccount.com \
  --confirm
```

輸出（`manifest_file_id` 是非秘密的 id，兩個設定檔都要填）：

```json
{
  "dry_run": false,
  "manifest_file_id": "<Drive file id>",
  "generation": 0,
  "folder_id": "<readview 資料夾 id>",
  "shared_with": "spike-reader@aistorage-spike-1-260926.iam.gserviceaccount.com",
  "permission_id": "<Drive permission id>"
}
```

接著把 `manifest_file_id` 寫進兩處：

- `config/committer.json`：`readview_manifest_file_id`（欄位已存在，預設 `null`）
- worker／讀取端設定（`~/.config/aistorage/reader.json` 或容器內的 `reader.json`）：
  `manifest_file_id`、`readview_folder_id`，以及 **`agora_folder_id`**（Agora 真本
  前綴 id；`agora checkout` 依 annex key 去那裡取原始紀錄，見步驟 2）

**怎麼驗證**

```bash
uv run python - <<'PY'
from pathlib import Path
from aistorage.drive import HttpDriveClient, RcloneConfToken
from aistorage.readview.model import parse_manifest
drive = HttpDriveClient(RcloneConfToken(Path.home()/".config/aistorage/rclone-committer.conf",
                                        remote="gdrive"))
m = parse_manifest(drive.download_bytes("<manifest file id>", max_bytes=1 << 20))
print("generation =", m.generation, "index =", m.index, "agora_main_sha =", m.agora_main_sha)
PY
```
`generation=0`、`index=None`、`agora_main_sha="unborn"` 是正確的初始狀態。
再用步驟 2 的 SA 片段讀一次，確認 SA 讀得到。

**失敗怎麼退**：manifest 還沒發佈過任何世代，所以 `drive.delete_permanently(<id>)`
之後把兩份設定檔的欄位改回 `null` 即可，沒有孤兒檔案。
（若已經發佈過世代就不是這樣了：那時要改讀取視圖必須走 recovery，不能刪 manifest。）

## 步驟 6｜在 `rclone-committer.conf` 設 `root_folder_id`

**誰做**：🛠 PM（直接編輯檔案）

> ⚠ **不要用 `rclone config update`**：那會重跑 OAuth 互動授權流程，可能要求重新同意，
> 甚至換掉 refresh token，等於把正式憑證作廢。要改就直接編輯 INI 檔（先備份）。

`root_folder_id` 是**圍堵用**的欄位：Drive API client 本身不吃它（呼叫端要自己給資料夾 id），
但 git-annex 的 rclone special remote 與任何 `rclone` CLI 都會受它限制——萬一 `rcloneprefix`
打錯，操作也出不會這個根。值＝步驟 1 的 `aistorage/` 資料夾 id。

```bash
CONF="$HOME/.config/aistorage/rclone-committer.conf"
cp -p "$CONF" "$CONF.bak.$(date +%Y%m%d%H%M%S)"   # ← 備份名稱，失敗時整份還原

python3 - "$CONF" "<aistorage/ 的資料夾 id>" <<'PY'
import configparser, sys
from pathlib import Path
conf, root_id = Path(sys.argv[1]), sys.argv[2]
p = configparser.ConfigParser(interpolation=None)   # 不做 interpolation（token 裡有 %）
p.read(conf, encoding="utf-8")
p["gdrive"]["root_folder_id"] = root_id
with conf.open("w", encoding="utf-8") as fh:
    p.write(fh)
print("已設定 root_folder_id（值不印）")
PY
# 這個 INI 只有 key=value（沒有需要保留的註解），configparser 重寫是安全的；
# 想完全保險就比對「把值遮掉」的兩份檔案：
# diff <(sed "s/=.*/=***/" "$CONF") <(sed "s/=.*/=***/" "$CONF.bak."*)
chmod 600 "$CONF"
```

**怎麼驗證**

```bash
rclone config file                                   # 確認讀到的是這一份
rclone lsf gdrive: --max-depth 1                     # 應該只列得出 aistorage/ 的內容
rclone lsf gdrive:agora --max-depth 1                # 真本前綴底下：manifest 與 bundle
```

```bash
# 圍堵欄位確實在檔案裡（只印行數，不印值）
grep -c '^root_folder_id' "$HOME/.config/aistorage/rclone-committer.conf"   # 應該是 1
```

**失敗怎麼退**：`cp -p "$CONF.bak.<時間戳>" "$CONF"`（備份檔名由上面 `cp` 產生），
再重跑一次驗證。

---

## 步驟 7｜放 GitHub Actions 的 secrets

**誰做**：👤 使用者（GitHub 網頁：`FATESAIKOU/MyAiStorage` → Settings → Secrets and variables → Actions）

| secret | 內容來源 | 備註 |
|---|---|---|
| `RCLONE_CONF` | `~/.config/aistorage/rclone-committer.conf` **整份檔案內容** | 只能整份貼上；**必須包含步驟 6 設的 `root_folder_id`** |
| `PIN_DEPLOY_KEY` | `~/.config/aistorage/pin-deploy-key`（OpenSSH 私鑰，含開頭結尾行） | 已經放過就確認仍在；不要重新產生（會對不上 pin repo 的公鑰） |

```bash
# 貼上之前先在本地檢查「有沒有換行問題」與「根欄位在不在」（不印內容）
python3 - <<'PY'
from pathlib import Path
t = (Path.home()/".config/aistorage/rclone-committer.conf").read_text()
print("有 [gdrive] 段:", "[gdrive]" in t)
print("有 root_folder_id:", "root_folder_id" in t)
print("行數:", len(t.splitlines()), "（貼到 web 時不要用會去掉換行的工具）")
PY
```

**怎麼驗證**：網頁上兩個 secret 都存在（只顯示名稱，值不可回看）。
刪掉再重貼是安全的（部署金鑰不變）。

**失敗怎麼退**：刪掉 secret。workflow 沒啟用（步驟 9 之後才啟用）之前放錯不會有任何副作用。

---

## 步驟 8｜產生 mac-opencode 的簽章金鑰並登錄

**誰做**：🛠 PM（產生金鑰）＋ commit（登錄檔是公開資訊，進 repo）

細節見 `docs/identity-setup.md`。私鑰**只在 Mac 上**，絕不進 repo。

```bash
cd /path/to/MyAiStorage
uv run python -m aistorage.identity keygen \
  --profile mac-opencode \
  --out ~/.config/aistorage/keys/mac-opencode.key     # 父目錄 700、檔案 600（工具會自動處理）
```

把印出來的 `key_id` 與 `public_key` 填進 `config/identity.json`（形狀見
`config/identity.example.json`）：

```json
{
  "format": "aistorage.identity/v1",
  "profiles": {
    "mac-opencode": {
      "allowed_types": ["session", "handoff", "claim", "continuation", "reference"],
      "signing_keys": [
        {"key_id": "mac-opencode-xxxxxxxx",
         "public_key": "（keygen 印出的 Base64）",
         "status": "active",
         "added_at": "<RFC3339>", "revoked_at": null}
      ],
      "inbox_folder_ids": ["（步驟 10 的收件匣 id）"]
    }
  }
}
```

```bash
uv run python -m aistorage.identity check config/identity.json
```

**怎麼驗證**：`check` 每個 profile 印出 `1 active key(s), 1 inbox folder(s)`；
且 `key_id` 的指紋與公鑰相符（`identity-setup.md` 步驟一的說明）。

**失敗怎麼退**：把 `config/identity.json` 還原後再 commit。金鑰輪替時
**不要刪舊金鑰**：`signing_keys` 保留舊筆目並把 `status` 改成 `revoked`、填 `revoked_at`
（`docs/identity-setup.md` 步驟四）。

> `allowed_types` 目前**不要**放 `artifact`：Agora 的提交流程不收產出登錄項目
> （ADR 0009），開了只會讓 artifact 進了收件匣卻被明確拒收（原因碼
> `artifact_not_supported`），而且每一輪都變成非空輪。

---

## 步驟 9｜worker 建立收件匣並登記

**誰做**：🛠 PM，但**必須用 worker 自己的憑證**建立收件匣

worker 的 OAuth client 是 `drive.file` scope，只看得見「**自己建立的**檔案」。
所以收件匣要由 worker 的憑證在它自己的空間建立，提交流程再依 id 去讀
（實測依據：`docs/spike/evidence/1.4-drive-file-isolation.md` 1.4i：profile 自己的 client
可以建收件匣，committer 依 id `files.get`／列舉成功）。反過來說，**不要**去共用別人
建立的收件匣資料夾來圖省事——那正是 1.4 揭露的注入路徑。

```bash
# (a) 用 worker 憑證建立收件匣（名稱帶 profile，別人一眼看得出是誰的）
cd /path/to/MyAiStorage
uv run python - <<'PYEOF3'
from pathlib import Path
from aistorage.drive import HttpDriveClient, RcloneConfToken
from aistorage.drive.model import GOOGLE_FOLDER_MIME

worker = HttpDriveClient(RcloneConfToken(
    Path.home() / ".config/aistorage/rclone-worker.conf", remote="gdrive"))
committer = HttpDriveClient(RcloneConfToken(
    Path.home() / ".config/aistorage/rclone-committer.conf", remote="gdrive"))

test_id = ""
for line in (Path.home() / ".config/aistorage/ids.env").read_text().splitlines():
    if line.startswith("TEST_FOLDER_ID="):
        test_id = line.split("=", 1)[1].strip()
account_root = worker.get(test_id).parents[0] if worker.get(test_id) else None
inbox = worker.create(account_root, "aistorage-inbox-mac-opencode", b"",
                      mime_type=GOOGLE_FOLDER_MIME)
print("inbox id =", inbox.id)

# (b) 立刻用 committer 身分確認「依 id 讀得到」；讀不到就停下來查
print("committer 看得到:", [f.name for f in committer.list_children(inbox.id)])
PYEOF3
```

> `worker.get(test_id)` 對 `drive.file` 應該是 404（它看不到別人建的），那個
> `if` 就是為了讓它在拿不到時改用別的方式取得帳號根；如果 404，就把 `account_root`
> 換成 committer 印出來的帳號根 id（`worker.get(...)` 的 404 正是預期行為）。

**怎麼驗證**

1. 上面的 `committer 看得到: []`（看得到資料夾本身、裡面是空的）＝權限對了。
2. 把 inbox id 填進 `config/identity.json` 的 `inbox_folder_ids`（步驟 8 已留欄位），
   並填進 worker／讀取端設定的 `inbox_folder_ids: {"mac-opencode": "<id>"}`。
3. `uv run python -m aistorage.identity check config/identity.json` 應該印出
   `1 active key(s), 1 inbox folder(s)`。
4. 上線後在容器內 `python -m aistorage.syncer opencode status --json` 讀得到收件匣狀態
   （不是「設定不足」）。容器需要的環境變數見 `resident/run.sh`：
   `AISTORAGE_PROFILE`、`AISTORAGE_RCLONE_CONF`（worker conf）、`AISTORAGE_SIGNING_KEY`、
   `AISTORAGE_SA_KEY`、`AISTORAGE_READER_CONFIG`。

**失敗怎麼退**：把 `config/identity.json` 的 `inbox_folder_ids` 清空後 commit，
`reader.json` 的同名欄位也清掉。收件匣裡如果留了測試項目，依 file id 永久刪除；
資料夾本體可以留著（沒有內容就不會被掃到）。

## 步驟 10｜啟用 committer workflow（排程每 6 小時）

**誰做**：👤 使用者（GitHub 網頁：`FATESAIKOU/MyAiStorage` → Actions → `committer` → Enable）

排程是 `.github/workflows/committer.yml` 裡的 `cron: "7 */6 * * *"`（每 6 小時的第 7 分），
不需要改檔案。workflow 需要兩個 secret（步驟 7）與 `PIN_DEPLOY_KEY`。

**怎麼驗證**

1. Actions 頁面 `committer` 顯示「Enable workflow」按鈕已消失（= 已啟用）。
2. 先不要等排程，直接用步驟 11 手動觸發一次。

**失敗怎麼退**：同一頁面 Disable。停用期間真本不會前進（同步器上傳的項目會留在收件匣），
之後重新啟用即可。

---

## 步驟 11｜log／artifact 保留天數設最短

**誰做**：👤 使用者（網頁：Settings → Actions → General → Artifacts and log retention）

設成最短（例如 1 day）。理由：runner 每輪都會印出 id、計數與耗時，這些 log 對除錯有用，
但**不是**真本，保留久了只是佔空間（Actions 儲存與 2.6 算的遠端儲存成本同一個家庭配額）。

**怎麼驗證**：同一頁的選單顯示已選的最短值。

**失敗怎麼退**：改成想要的保留天數（純設定，無副作用）。

---

## 步驟 12｜第一次手動觸發與驗收

**誰做**：🛠 PM（觸發）＋ 雙方一起看結果

```bash
# (a) 手動觸發
gh workflow run committer.yml --repo FATESAIKOU/MyAiStorage
gh run list --repo FATESAIKOU/MyAiStorage --workflow committer.yml --limit 1

# (b) 看這一輪的摘要（job 失敗時看 log 的最後 40 行）
gh run view --repo FATESAIKOU/MyAiStorage --log-failed | tail -40
```

第一次觸發時**收件匣是空的**，`prescan` 會回 0，後面所有步驟都跳過——這是預期行為，
不是失敗。想看完整一輪，先在 Mac 上做一次「同步並提交」：

```bash
# 在 Mac 上（暫存目錄內的 clone 由工具自己管理）
cd /path/to/MyAiStorage
AISTORAGE_RCLONE_CONF="$HOME/.config/aistorage/rclone-committer.conf" \
AISTORAGE_PIN_KEY="$HOME/.config/aistorage/pin-deploy-key" \
AISTORAGE_PIN_KNOWN_HOSTS="config/github_known_hosts" \
uv run python -m aistorage.committer run --config config/committer.json --dry-run
```
`--dry-run` 不寫入、不推送，只會把 13 步的計畫印出來。確認無誤後去掉 `--dry-run` 才真的跑。

**驗收清單（全部要成立）**

| # | 檢查 | 指令／位置 | 預期 |
|---|---|---|---|
| 1 | workflow 成功 | `gh run list` | 綠色、沒有 aborted |
| 2 | 釘選值轉正 | `gh api repos/FATESAIKOU/MyAiStorage-pin/contents/.pin --jq '.[].name'` | 有 `agora.json`、**沒有** `agora.pending.json` |
| 3 | 真本前綴正常 | 步驟 3(b) 的 `list_children` | 有 `GITMANIFEST--<uuid>` 與 ≥1 個 `GITBUNDLE-*` |
| 4 | 讀取視圖有世代 | 步驟 5 的驗證片段 | `generation` 從 0 變 1、`index` 不再是 `None` |
| 5 | 收件匣被清空 | `list_children(<inbox id>)` | 空 |
| 6 | 隔離區是空的 | `list_children(<agora-quarantine id>)` | 空（有東西就是被判成注入，要查） |
| 7 | 讀取端讀得到 | 容器內 `python -m aistorage.reader find --text` 或 syncer `status` | 有讀到 manifest |
| 8 | 沒有誤隔離 | 步驟 6 的 `rclone lsf gdrive:agora` | 檔案清單與 manifest 一致 |

**失敗怎麼退**：workflow 紅了先看 `gh run view --log-failed` 的 `aborted_at`／`code`。
常見三種：

- `MismatchError`（釘選值與遠端不符）：**不要手改 pin**，照 `docs/runbooks/recovery.md`。
- `maintenance`（管理操作中）：`python -m aistorage.admin lock-status --pin-repo …` 看旗標，
  處理完 `python -m aistorage.admin unlock --confirm`。
- 憑證／網路類錯誤：修好後 `gh workflow run committer.yml` 重跑；**收件匣是冪等的**，
  重跑不會重複收（會記成 `already`）。

要「完全不要有資料進來」時：Disable workflow（步驟 10 的退法）。

---

## 步驟 13｜健康檢查的 launchd（**需要使用者明確同意**）

**誰做**：👤 使用者（決定）＋ 🛠 PM（執行）

```bash
# (a) 先看將要安裝的 plist 內容（不安裝）
uv run python -m aistorage.admin health --plist

# (b) 使用者同意後才安裝（每 6 小時跑一次，失敗時 exit 1 並可通知）
sh scripts/install-health-launchd.sh
```

`--plist` 印出來的指令是 `uv run python -m aistorage.admin health`：它會自己
`collect_health()` 取資料再判定，所以**不需要** `--data-json`（舊版 plist 缺參數會讓
每 6 小時都以 AdminError 結束、永遠不通知）。

**怎麼驗證**

```bash
launchctl list | grep local.aistorage.health     # 有 pid／last exit status
tail -20 /tmp/aistorage-health.log               # 判定結果（JSON）
```

**失敗怎麼退**（兩條都跑，或使用者想停就只跑第一條）

```bash
launchctl bootout gui/$UID/local.aistorage.health
rm ~/Library/LaunchAgents/local.aistorage.health.plist
```

沒有 launchd 也能用：`uv run python -m aistorage.admin health --config config/committer.json --notify`
（手動跑；判斷項目與閾值見 `docs/runbooks/health.md`）。

---

## 附錄 A：上線當天要填的對照表（填完貼回 `docs/resources.md`）

| 項目 | id／值 | 填在哪裡 |
|---|---|---|
| `aistorage/`（共同根） | | `docs/resources.md` |
| `aistorage/agora/` | | `config/committer.json` `prefix_folder_id` |
| `aistorage/agora-quarantine/` | | `config/committer.json` `quarantine_folder_id` |
| `aistorage/readview/` | | `config/committer.json` `readview_folder_id` |
| 讀取視圖 manifest（`admin init-readview --confirm` 印出） | | `config/committer.json` `readview_manifest_file_id` ＋ `reader.json` `manifest_file_id` |
| `aistorage/`（Agora 真本前綴，annex 物件） | 唯讀分享給讀取用 SA（步驟 2；`agora checkout` 取原始紀錄用） | `reader.json` `agora_folder_id` |
| `aistorage-inbox-mac-opencode/`（worker 自建，步驟 9） | | `config/identity.json` `inbox_folder_ids` ＋ `reader.json` `inbox_folder_ids` |
| Agora annex remote uuid | | `config/committer.json` `repo_uuid`、`repo_url`（`annex::<uuid>?…&rcloneprefix=aistorage/agora`） |
| `root_folder_id` | | `rclone-committer.conf`（步驟 6） |

`config/committer.json` 改完要 **commit 並 push 到 `main`**——workflow 的 guard 步驟會
比對這一個 sha，沒有 push 的設定不會生效。

## 附錄 B：常見失敗與退法（速查）

| 症狀 | 原因 | 退法 |
|---|---|---|
| `init-pin` 報找不到唯一主 manifest | 步驟 3 沒推上去，或 `rcloneprefix` 寫成 id | 刪掉前綴內的檔案，重做步驟 3（`rcloneprefix` 只能是名稱路徑） |
| `init-pin` push 失敗（non-fast-forward） | pin repo 被別的流程寫過 | `gh api` 刪掉 `.pin/agora.pending.*` 後重跑；`--confirm` 前都還沒正式值 |
| workflow 每次都 `maintenance` 中止 | pin repo 有殘留旗標 | `python -m aistorage.admin lock-status --pin-repo git@github.com:FATESAIKOU/MyAiStorage-pin.git`；處理完 `unlock --confirm` |
| 改了 conf 之後憑證失效 | 誤用 `rclone config update`（會重跑 OAuth 授權流程） | 用步驟 6 的備份檔還原 conf；refresh token 真的被換掉才需重新授權 |
| 讀取端 `AccessDenied`／manifest 404 | 沒分享給 SA（`init-readview` 沒帶 `--sa-email`），或 id 填錯 | 用 `init-readview --dry-run` 看計畫；已建立就手動分享資料夾給 SA，再確認 id |
| 同步器「設定不足」／讀不到收件匣 | worker 環境變數、金鑰沒掛，或收件匣不是 worker 自己建的 | 對照 `resident/run.sh` 的環境變數清單；`python -m aistorage.syncer opencode status --json` 驗證 |
| 每輪都非空輪 | 收件匣有項目一直不被接受 | 看拒收紀錄（讀取視圖的 `rejections`）；常見是 `allowed_types` 沒開或 key_id 不在 `config/identity.json` |
