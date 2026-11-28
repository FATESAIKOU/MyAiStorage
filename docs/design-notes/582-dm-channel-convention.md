# #582 專案×角色頻道慣例（MyAiEntry N34 依據）

- 單：#582；PR #583。作者：專案架構師（作者模式，GPT-6 / Codex），2026-11-28。
- 基準：`a1daf3c`。只改設計文件；不改程式、主 spec 或頻道設定。
- **本人 11-28 裁定（經 PMO）**：每個專案×角色共用一個頻道，由 PMO 建立；頻道不帶 team 或席位序號，在頻道內以完整 agent id 點名實際運作的 AI。D-39 同步本模型；命名與先開哪些角色是本設計建議，待重審。

## 0. 結論與舊案追溯

採 `role-<project>-<role>`，先為現有八個專案各開 PM 與 arch，合計 16 個頻道；同專案的 w1、w2 與不同 arch 序號共用對應角色頻道。這是角色歷史與對話入口，不能再依 `(from, to)` 當成真正雙人 DM，也不能把頻道當成 agent id 或權限邊界。

舊 A／B／C 比較**已被本人裁定取代**，僅保留追溯摘要：A 在既有 `team-*` 做雙人視圖，零新增實體但混入團隊歷史且無法區分普通點名；B 每對一頻道 `dm_<a>_<b>`，可區分對話但隨席位換代增殖、增加全員輪詢成本；C 共用 `dm-secretary`，只增一個實體但把所有角色對話聚在一起。前版建議 C 已撤回，不建立這三案的頻道，也不沿用「全域 @你排除 DM」的特殊計數規則。角色頻道仍依正常 `to`／`mentions` 計入 @你，App 若另列角色頻道，避免把同一訊息重複計數即可。

## 1. 命名與註冊簿

### 1.1 名稱可逆、與既有命名分離

建議 `role-<project>-<role>`：`project`、`role` 取自 `src/ainame/__init__.py` 的 `Name.project`、`Name.role`，角色限 `ROLES = (pm, arch, impl, test, rev, secretary)`。ainame 沒有專案白名單，不能把現有八個專案誤寫成它的 enum。

編碼由合法 ainame 解析後取這兩欄。反解先去掉固定 `role-`，再以最後一個 `-` 切出角色；專案可含 `-` 或角色字，所以不能從左邊找角色字。例如 `pm-tools-impl-w1-1` → `role-pm-tools-impl` → `(pm-tools, impl)`。回驗可用最短合成席位：single role 用 `<project>-<role>-a`，其餘用 `<project>-<role>-a-1`，以 `ainame.parse` 驗證兩欄一致。

名稱滿足 `messages.py` 與 `registry.py` 的 `CHANNEL_NAME_RE = ^[A-Za-z0-9_-]+$`，`channel_slug` 對此小寫名稱原樣通過。Angareion 頻道驗證沒有另外的長度上限；ainame 的 `MAX_LEN=32` 是完整席位上限，不能直接套給頻道名。從合法席位移除至少 `-a`、加上 `role-`，角色頻道最長 35 字（有序號角色最長 33 字）。目前 PM／arch 的實際清單更短。固定 `role-` 前綴不會與 `team-*` 或 `secretary-pmo` 撞名，也不會把同專案不同 team 變成不同角色頻道。

### 1.2 沿用 schema 1，零改碼

註冊留言仍為：

```json
{"schema":1,"action":"register","channel":"role-myaistorage-pm","backend":"aicompany-channel","issue":123}
```

`123` 只是結構示例，不是實際 issue。`registry.parse_comment` 嚴格拒絕未知欄位；加 `kind` 目前會得到 `unknown fields`。角色類型由可逆名稱辨識，**不加 `kind`、project、role 或 members 欄**。`registry_rules.rebuild_channels` 依授權的註冊／取消／換頁留言重建 `{backend, issue, retired?}`，workflow 寫回 `config/angareion/channels.json`；`channel_config.ChannelStore` 讀此映射。設定檔是生成物，不手改；PMO 執行建立指令後等映射落地，`channel create` 的等待上限為 120 秒，不能因超時就盲目重建。

每個角色頻道套用 #542 的換頁，並保留固定邏輯名稱；建頻道與換頁仍消耗共用註冊簿留言、全體 `read` 仍掃描所有已登記頻道。16 個入口有持續輪詢成本，故不先擴張到所有角色。

## 2. 開哪些角色、成員與 App 名冊

先開 **PM 與 arch**：PM 是協調入口，arch 保存跨工單的設計脈絡，兩者有持續角色用途。impl／rev／test 多按單短期開立，id 的 team 與 seq 經常換，單有角色頻道仍不知道該 mention 哪一位；預設沿用團隊頻道與工單，需要本人詢問時由 PMO 指路，不為每次換號建立新頻道。secretary 已有專門派工入口，首批不另開角色頻道。日後增開仍用同一命名，由 PMO 按需求建立，不把新席位出現當成自動建頻道條件。

**註冊簿不存成員**：它描述頻道位置，並沒有加入／退出、訂閱或 ACL。所有持有通道憑證的讀者都可讀頻道歷史；一般收件是訊息位址過濾，不是成員權限。

App 的 @ 選單依期九 v3 使用 App 已有的**各 sidecar `GET /agents`**，按 ainame 的 project／role 篩選，顯示完整 id、team、seq、所在 sidecar 與 state。`idle`、`running` 為目前可 poke 的對象；`starting`、`waiting-quota`、`exited` 不能當成可立即叫醒。名冊只輔助確認角色分工／主責，不是即時存活證據；快照之後仍可能退出，送出與 poke 結果須分開顯示。sidecar 無法連線時標為未知，不能把歷史名冊冒充在跑清單。

同 id 出現在多台 sidecar，要顯示重複部署提示，不能把它們誤認為不同位址；訊息仍只有一個 id 位址，aicomm 的跨機查找可能 poke 多台。同一身分只留一個收信讀者的規則不變。App 實際 UI 與 sidecar 名冊整合不在本 repo 實測範圍。

## 3. 收件、mention、叫醒與版本前提

### 3.1 收件與叫醒是兩件事

`backend.recipient_matches`：一般收信條件為 `to is None OR to == me OR me in mentions`；`--mentioned` 只收後兩項。`aicomm inbox` 固定轉成不帶 `--channel` 的 `angareion read`。`read --channel` 則讀該頻道全體，stderr 會警告；`angareion log` 不加位址過濾亦可查他人訊息，`aicomm log` 預設補 `--to me`。沒有後端 ACL，不可放私人資訊。

| 送法（本人寄入角色頻道） | 指定 PM 一般 read／inbox | 其他人一般 read／inbox | 其他人 --mentioned | aicomm 叫醒對象 |
|---|---|---|---|---|
| 只有 `--mention <pm-id>`、沒有 `--to` | 收到 | 新廣播也收到 | 不收到 | 該 PM |
| `--to <pm-id>`（可同時 mention 同 id） | 收到 | 收不到，除非另被 mention | 不收到 | 該 PM，一次 |
| 沒有 to／mention | 新廣播收到 | 新廣播收到 | 不收到 | 無 |

第一讀的新身分只收「現在」以後的普通廣播，點名自己的舊信仍從頭收，所以不能用第三者第一次讀空值，宣稱 mention-only 隔離成功。§5 先替兩個 PM 建立游標，再送 mention-only，驗證另一個 PM 也收到。廣播可送到其他專案的一般 inbox，並不限同角色的人。

建議 App 正常點名時同時寫 **`to: <完整 agent id>` 與 `mentions: [<同 id>]`**（只帶 `--to` 亦可遞送）；`from` 是 `myaientry-secretary-p1`，`channel` 是所選 `role-…`，回覆已有訊息時再帶 `in_reply_to`。內文 `@名字` 只是文字。若使用者刻意只 mention 不帶 to，應明示這是廣播，不能宣稱其他人一般收不到。App 仍顯示整個角色頻道歷史，不依收件人把它拆成永久 DM。

`angareion send` 本身不 poke。`src/aicomm/cli.py::_cmd_send` 合併 `args.to` 與 `args.mention`，去重並排除自己，成功寫入後逐一 `_wake_one`；**wake 不是只看 `--to`**。只帶 mention 也會叫醒；`--no-wake` 則不叫醒。現行 wake 只對 idle／running 發 poke，預設 running 的 poke 可排隊；exit 0 表示送達，不能當作叫醒成功，更不能因 poke 失敗重送。App 若直接寫 Angareion，須沿用其既有 sidecar poke 流程，不能假設有 mentions 欄就自動叫醒；本單不新增 App 實作。

### 3.2 「讀者先升級」限制的來源與現況

已搜尋 `docs/`（含 `docs/design-notes/`）、`skills/`、`openspec/` 與版本歷史。原限制出自 #209（`1e69b31`，合併 `99db372`）：讀者需能解析 `mentions`，並把自己在 mentions 內的訊息納入 recipient／mentioned 過濾；不是單純新增 sender CLI 旗標，也不是 #542 的換頁能力。

歷史版 `docs/angareion.md` 有「順序：讀者先升級、再送 mentions」；歸檔的 `openspec/changes/archive/2026-10-25-comm-skill/design.md` P7 與升級順序仍保留當時警告。#234 的 `04c1c69`（後合併 `c300ef8`）已把現行文件改為「`--mention` 可以用了」，記載期 4 M2 tasks 2.4 於 **2026-10-25** 完成全讀者升級；目前 `docs/angareion.md`、`skills/angareion/SKILL.md`、`skills/aicomm/SKILL.md` 與 CLI help 一致。故舊的全面禁用限制**已解除**；新加入的讀者種類仍須確認理解 mentions。

`angareion: 1` 訊息格式未升版；套件版本號不能單獨證明能力，文件建議看 `--version` 的來源 commit。此 checkout 的 `--capabilities` 只有 `paged-read paged-write`（`cli.CAPABILITIES`），**沒有 mention 能力旗標**，不能用換頁旗標替代 mention 相容性證明。

**部署 pin 已核對、全艦隊實機仍未驗**。唯讀查得共用 worker 的 MyAiStorage pin 為 `1bf38828540bf1616d437cd4f54ca4390b271554`；以 git 確認它包含 #209 合併 `99db372`，並直接核對該 pin 的 `backend.py` 有 `recipient in message.mentions` 與新身分的 mention 收件處理。故該已宣告 pin 滿足 Python 讀者的 mentions 能力，不是依日期猜測。此核對只證明部署設定，沒有逐台比對運行映像，也沒有核對所有非 Python 讀者（含 App）目前版本；**11-28 全艦隊實際部署是否全部滿足仍未驗**。文件支持 10-25 已全讀者升級，推論未回退且沿用該 pin 的 Python 部署可用；PMO 加入新讀者或驗收部署時仍須核對各讀者種類及實際來源版本，未知讀者優先用 `to`，不重新宣告全域禁止 mention。

## 4. AI 回覆與多席位協調

AI 只有收到本人點名（自己的 `to`／`mentions`）才以被詢問者身分回答；一般 read 看到廣播或顯式讀頻道看到別人的話，不代表被指派。回覆**同一個角色頻道**，`--to myaientry-secretary-p1 --in-reply-to <原訊息 id>`，讓 App 的 @你與對話串都能對上；可同時 mention 本人，但不會因此在 sidecar 叫醒本人。

```sh
aicomm send --channel role-myaistorage-pm --to myaientry-secretary-p1 --in-reply-to <原訊息id> --body '目前進度與答案'
```

「AI 不主動找本人、要本人決定的事經 PMO」不變；此入口只允許回應本人先發的詢問，不增加主動請示或派工權限。正式秘書／PMO 派工仍走 `secretary-pmo`，角色頻道的自由文字不轉成派工單。`pmo` 目前仍會對寄給 `mypmo-pm-m1` 的本人訊息觸發既有 call／機械 ack（見 `pmo/inbox.py`、`round.py`），角色頻道不豁免此行為。

同角色多位同時在跑時，**被完整 id 點名者回**：例如 w1 與 w2 的 PM 共用 `role-myaistorage-pm`，點名 w1 不讓 w2 代答。若明確點名多位，各自只答自己的部分；要求單一整合答案時由本人指定主答者。沒有指定有效 id 時，不讓所有同角色 AI 搶答，App 提示選人，PMO 協調主責；收件前退出的 AI 亦不由另一位自動冒名接收。頻道與歷史不因席位替換重建，後任要補看舊席位脈絡可顯式查角色頻道（保留其讀全體語意）。

## 5. 隔離驗證與可重現輸出

手動驗證腳本放團隊目錄的 `verify-582/verify_role_fake.py`，不放 repo、不存入 agent 資料目錄；執行時只在同一個 `verify-582/` 下建立隔離暫存子目錄並自動清除。腳本在同一行程先設定 HOME、TMPDIR、Agora 與 Angareion 路徑，清掉所有 XDG_* 與規定的外來設定／身分變數，再 assert `Paths.from_env()` config／cache／state、HOME、TMPDIR、Angareion 路徑全部在隔離根下，不符即 `NOT ISOLATED` 中止。只使用 FakeBackend、FakeGit、FakeRegistryClient 重建映射，不啟動 agent、不連真 Drive 或 GitHub。

從 repo 根執行（`<團隊目錄>` 由 PMO 提供）：

```sh
uv run python <團隊目錄>/verify-582/verify_role_fake.py > <團隊目錄>/verify-582/role-output.txt 2>&1
```

fake 的兩個 id 模擬正在使用的 PM 位址；**沒有對真實「在跑」agent 或 sidecar 做端到端測試**。實際 angareion CLI 的建頻道、收件、廣播與定向分流均有斷言；aicomm send／inbox 以相同 CLI 接 fake backend，攔截 `_wake_one` 記錄目標，僅證明 mention 會進入叫醒候選，不能證明真 sidecar 已叫醒。App、真 GitHub、全艦隊 pin 與換頁不在本次實測範圍。

以下為成功執行的合併 stdout／stderr 原樣輸出（若有隔離絕對路徑以 `<暫存目錄>` 代換）：

```text
ISOLATION OK: HOME, TMPDIR, Paths.config/cache/state and Angareion paths under <暫存目錄>

$ ANGAREION_IDENTITY=mypmo-pm-m1 angareion channel create role-myaistorage-pm --backend aicompany-channel
exit=0
stdout:
role-myaistorage-pm
REBUILT {
  "channels": {
    "role-myaistorage-pm": {
      "backend": "aicompany-channel",
      "issue": 100
    }
  },
  "version": 1
}

$ ANGAREION_IDENTITY=myaistorage-pm-w1 angareion read --json
exit=0
stdout:
[]

$ ANGAREION_IDENTITY=myaistorage-pm-w2 angareion read --json
exit=0
stdout:
[]

$ ANGAREION_IDENTITY=myaientry-secretary-p1 angareion send --channel role-myaistorage-pm --mention myaistorage-pm-w1 --body mention-only
exit=0
stdout:
01M8GFB9DX3ZMNBFSEKH3ZETT2

$ ANGAREION_IDENTITY=myaistorage-pm-w1 angareion read --json
exit=0
stdout:
[
  {
    "id": "01M8GFB9DX3ZMNBFSEKH3ZETT2",
    "at": "2026-11-28T07:01:39.389796+00:00",
    "from": "myaientry-secretary-p1",
    "to": null,
    "channel": "role-myaistorage-pm",
    "urgency": 5,
    "content": "mention-only",
    "attachments": [],
    "mentions": [
      "myaistorage-pm-w1"
    ],
    "in_reply_to": null,
    "datatype": "message",
    "received_at": "2026-11-28T02:00:08Z"
  }
]

$ ANGAREION_IDENTITY=myaistorage-pm-w2 angareion read --json
exit=0
stdout:
[
  {
    "id": "01M8GFB9DX3ZMNBFSEKH3ZETT2",
    "at": "2026-11-28T07:01:39.389796+00:00",
    "from": "myaientry-secretary-p1",
    "to": null,
    "channel": "role-myaistorage-pm",
    "urgency": 5,
    "content": "mention-only",
    "attachments": [],
    "mentions": [
      "myaistorage-pm-w1"
    ],
    "in_reply_to": null,
    "datatype": "message",
    "received_at": "2026-11-28T02:00:08Z"
  }
]

$ ANGAREION_IDENTITY=myaientry-secretary-p1 angareion send --channel role-myaistorage-pm --to myaistorage-pm-w1 --mention myaistorage-pm-w1 --body targeted
exit=0
stdout:
01M8GFB9E8TZ79WGC6KDMT9H5D

$ ANGAREION_IDENTITY=myaistorage-pm-w1 angareion read --json
exit=0
stdout:
[
  {
    "id": "01M8GFB9E8TZ79WGC6KDMT9H5D",
    "at": "2026-11-28T07:01:39.400394+00:00",
    "from": "myaientry-secretary-p1",
    "to": "myaistorage-pm-w1",
    "channel": "role-myaistorage-pm",
    "urgency": 5,
    "content": "targeted",
    "attachments": [],
    "mentions": [
      "myaistorage-pm-w1"
    ],
    "in_reply_to": null,
    "datatype": "message",
    "received_at": "2026-11-28T02:00:12Z"
  }
]

$ ANGAREION_IDENTITY=myaistorage-pm-w2 angareion read --json
exit=0
stdout:
[]

$ ANGAREION_IDENTITY=myaistorage-pm-w2 angareion read --channel role-myaistorage-pm --json
exit=0
stdout:
[
  {
    "id": "01M8GFB9DX3ZMNBFSEKH3ZETT2",
    "at": "2026-11-28T07:01:39.389796+00:00",
    "from": "myaientry-secretary-p1",
    "to": null,
    "channel": "role-myaistorage-pm",
    "urgency": 5,
    "content": "mention-only",
    "attachments": [],
    "mentions": [
      "myaistorage-pm-w1"
    ],
    "in_reply_to": null,
    "datatype": "message",
    "received_at": "2026-11-28T02:00:08Z"
  },
  {
    "id": "01M8GFB9E8TZ79WGC6KDMT9H5D",
    "at": "2026-11-28T07:01:39.400394+00:00",
    "from": "myaientry-secretary-p1",
    "to": "myaistorage-pm-w1",
    "channel": "role-myaistorage-pm",
    "urgency": 5,
    "content": "targeted",
    "attachments": [],
    "mentions": [
      "myaistorage-pm-w1"
    ],
    "in_reply_to": null,
    "datatype": "message",
    "received_at": "2026-11-28T02:00:12Z"
  }
]
stderr:
Warning: --channel reads every message in the channel, including messages addressed to other agents; omit --channel to get only messages addressed to you.
$ aicomm send --channel role-myaistorage-pm --mention myaistorage-pm-w1 --body aicomm-mention
01M8GFB9FSESHQJZQ6NZFJV68G
fake-wake myaistorage-pm-w1
FAKE_WAKE_TARGETS=["myaistorage-pm-w1"]
$ aicomm inbox --json (myaistorage-pm-w1)
[
  {
    "id": "01M8GFB9FSESHQJZQ6NZFJV68G",
    "at": "2026-11-28T07:01:39.449245+00:00",
    "from": "myaientry-secretary-p1",
    "to": null,
    "channel": "role-myaistorage-pm",
    "urgency": 5,
    "content": "aicomm-mention",
    "attachments": [],
    "mentions": [
      "myaistorage-pm-w1"
    ],
    "in_reply_to": null,
    "datatype": "message",
    "received_at": "2026-11-28T02:00:16Z"
  }
]
ALL CHECKS PASSED (fake wake assumes eligible running/idle agent; no real sidecar)
```

## 6. PMO 建立清單與交付邊界

現有 `channels.json` 的團隊頻道推得專案：life、llmgateway、myaientry、myaistorage、mycodequality、mycostcontrol、mylinuxpool、mypmo。以相應最短席位可通過 ainame 驗證；其中 `team-mylinuxpool` 是歷史無 team 名稱，`team-mylinuxpool-wN-b`、`team-mypmo-m1-b` 是既有另用頻道，**不**因此多推 `mylinuxpool-wN` 或 `mypmo-m1` 專案。test／實驗頻道不算專案。

以下 16 個頻道目前尚未出現在設定中；由 PMO 在有註冊簿授權與 channel token 的環境建立，本設計未執行真實建立。執行前重新查映射，已存在就跳過，不刪除重建；等重建落地後交 App 刷新。

```sh
angareion channel create role-life-pm --backend aicompany-channel
angareion channel create role-life-arch --backend aicompany-channel
angareion channel create role-llmgateway-pm --backend aicompany-channel
angareion channel create role-llmgateway-arch --backend aicompany-channel
angareion channel create role-myaientry-pm --backend aicompany-channel
angareion channel create role-myaientry-arch --backend aicompany-channel
angareion channel create role-myaistorage-pm --backend aicompany-channel
angareion channel create role-myaistorage-arch --backend aicompany-channel
angareion channel create role-mycodequality-pm --backend aicompany-channel
angareion channel create role-mycodequality-arch --backend aicompany-channel
angareion channel create role-mycostcontrol-pm --backend aicompany-channel
angareion channel create role-mycostcontrol-arch --backend aicompany-channel
angareion channel create role-mylinuxpool-pm --backend aicompany-channel
angareion channel create role-mylinuxpool-arch --backend aicompany-channel
angareion channel create role-mypmo-pm --backend aicompany-channel
angareion channel create role-mypmo-arch --backend aicompany-channel
```

本 PR 只交設計與 D-39，PMO 建頻道、App N34 的角色頻道入口／名冊與 poke 接線、必要的使用說明更新由各負責方依此契約排程；不擅改訊息格式或註冊 schema。待重審的建議包括 16 個首批入口與 App 預設 to＋mention，不再把已裁定的「專案×角色」退回 A／B／C 選案。
