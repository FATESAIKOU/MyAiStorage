# `atelier` 指令（v0 工具；PR 1）

讀 Atelier harness 樹（`atelier/<team>/<role>/v<N>.md`），並把一份 harness 裝進 agent 的使用者層級指示檔。與 `agora`／`angareion` 同一家族（同一個 distribution，`uv tool install` 後就有 `atelier`）；設計全文：`openspec/changes/archive/2026-10-17-atelier-v0/`（現行規格在 `openspec/specs/atelier/spec.md`）。

## 指令

```
atelier ls [--json]
atelier show <id>[@<版本>] [--section know|do|judge|dont|capabilities] [--json]
atelier install <id>[@<版本>] --agent <claude|opencode> [--agent …] [--global]
atelier render <id>[@<版本>] [--append <檔>] --out <檔>
```

- `<id>` 是 `atelier:<team>-<role>`（例 `atelier:worker-team-pm`）；`@v<N>` 不給＝同一職務檔案裡最大的 N。
- `ls`：一個版本一行 `atelier:worker-team-pm@v0`；`--json` 給 `[{"id","team","role","version"}, …]`（照 team／role／版本排序）。
- `show`：預設整份**原樣 byte-for-byte**（含 frontmatter；就是 `git show` 的位元組，不整理、不加行）；`--section` 只印該章節本文（不含 `##` 標題、去頭尾空行，行內原樣）；`--json` 給 `{"id","team","role","version","frontmatter","sections"}`，跟 `--section` 一起用時給 `{"id","team","role","version","section","content"}`。
- `install`：`--agent` **可以給多次**；每個值各裝一次、與順序無關、同值只裝一次；**不給＝exit 1**。不指定版本＝裝最大 N。`v1` 起對 `role: pm` 的 harness，預設拒絕（exit 1，訊息指向 `render`／由啟動方交付；說明同一個使用者的所有 agent 都會拿到 PM 規矩）；只有明寫 `--global` 時才安裝，並在 stderr 印一行警告「同一個使用者的每個 agent 都會拿到這份 PM harness」（整台機器只有一個 agent 時才該用）。`v0` 與非 pm 的 harness 不受影響。
- `render`：產生**單一個 agent** 的指示檔（`--out` 必給，**不給或空字串＝exit 1**）；不指定版本＝最大 N；`--append` 的檔是接在 harness 後面的環境說明。
- 退出碼：
  - `show`／`install`：**0** 成功；**1** 用法錯、找不到 id／版本、政策拒絕（`v1` 的 pm harness 沒有 `--global`）；**2** 複本或檔案問題（讀取失敗、格式壞、寫不進去）。
  - `render`：**0** 成功；**1** 用法錯（參數錯、壞 id 格式、缺 `--out`、`--out ''`、`--append` 檔不存在含 `--append ''`）；**2** 複本不存在、git 或檔案讀取失敗、harness 格式壞、`--append` 讀不到、`--out` 寫不進去；**3** harness 查無 id／版本（`HarnessNotFound`，讓呼叫方能分辨「查無」與「用法錯」；查無時 `--out` 不會被建立）。訊息不含 harness 內文。

## 複本與 fetch（與 angareion 共用）

- 讀取走本機複本（MyAiStorage 的 clone）：`git fetch origin main` 更新、`git show origin/main:<路徑>` 讀檔；**不依賴工作樹**（本機改動、detached HEAD 都無妨）、匿名 HTTPS、不用憑證。
- **複本路徑**：`ATELIER_CONFIG_DIR`；沒設就看 angareion 的 `config.json`（`ANGAREION_CONFIG`，預設 `~/.config/angareion/config.json`）裡的 `config_dir`——兩邊共用同一份複本。
- **同一把 flock**：`<複本>/.git/angareion-pull.lock`（angareion 用的同一個鎖檔，兩邊的 fetch 因此不並行）；git 逾時 **20 秒**。
- fetch 節流：指令開始時距上次 fetch（`FETCH_HEAD` 的 mtime）超過 `config_pull_interval` 才拉——**沿用 angareion `config.json` 的 `config_pull_interval`**（沒設＝15 分鐘／900 秒；`0`＝不按週期拉，查無時的事件驅動 fetch 仍會做）；查無 id／版本時再補一次（距上次不到 30 秒就跳過）。**fetch 失敗只警告一次（stderr）並沿用目前的複本，退出碼不變。**
- 唯一的本機寫入就是複本自身的 `git fetch`（以及它建立的共用鎖檔 `<複本>/.git/angareion-pull.lock`）、install 的目標檔，和 render 的 `--out`；工具不會寫 harness 樹、工作目錄或使用者其他檔案。

## install 的目標與管理區塊

- **claude**：獨立檔 `<CLAUDE_CONFIG_DIR 或 ~/.claude>/rules/atelier-<team>-<role>.md`，內容＝harness 原樣（一個職務一個檔，不碰別人的檔）。
- **opencode**：`<XDG_CONFIG_HOME 或 ~/.config>/opencode/AGENTS.md` 裡的**管理區塊**：

  ```
  <!-- atelier:worker-team-pm -->
  （harness 原樣）
  <!-- /atelier:worker-team-pm -->
  ```

- **冪等**：同一個 harness 的區塊被替換（重跑不堆疊）、區塊外的內容逐字保留（含 `\r\n`；讀寫都用位元組）；目標檔不存在就建立；寫入走同目錄暫存檔＋`os.replace`（中斷只會留下舊檔，不會半寫）。只有**整行、成對**的標記、**且**中間是可解析、`type: Harness` 的 frontmatter 區塊（= 兩個整行標記＋內文以 frontmatter 開頭），才會被當成我們的區塊；其他**壞檔拒絕**（原檔不動、exit 2）：不成對（有開頭沒結尾、只有結尾）或成對但不是我們的區塊。行內提到標記不算標記，你自己內容裡的標記行不會被吃掉。
- **install 先驗**：要裝的 harness 必須有可解析的 frontmatter 且 `type: Harness`；沒有（或不是）＝exit 2、**什麼都不寫**。opencode 另外拒絕內文含整行管理標記的 harness（裝了會讓之後的 install 拒絕該檔）。
- **不碰**：`~/.claude/settings.json`（也絕不產生 `apiKeyHelper`）、專案自己的 `AGENTS.md`／`CLAUDE.md`、工作目錄的任何檔案。
- 生效：claude 在下一個新 conversation（`--resume` 時是否重載要在驗收確認）；opencode 依官方文件在下一個模型請求前。期 1 由 MyLinuxPool 的開機掛點跑 `atelier install atelier:worker-team-pm --agent claude --agent opencode`（PR 3；sidecar 不改）。
- 職務檔放在 `atelier/<team>/<role>/v<N>.md`（例如 `atelier/worker-team/pm/v0.md`，識別 `atelier:worker-team-pm@v0`）：`atelier ls` 會列出複本上所有的版本。

## render：交給單一個 agent 的指示檔（`v1` 起的交付方式）

- **和 install 的差別**：install 寫**使用者層級**指示檔，同一個使用者的**每個** agent 都會拿到（`v0` 用它）；**render 產生一個檔，給「啟動單一個 agent 時交付」用**——啟動方（worker 的 sidecar、Mac 的開 pane 指令／`agora continue`）在起 agent **之前**跑 `atelier render <id> --append <環境說明> --out <那個 agent 自己的 state 目錄>/instructions.md`，再把那個檔交給該 process（各 agent 的『附加系統指示檔』能力，旗標見環境說明）。**render 自己不啟動 agent，也不寫使用者層級**；`render` 回 1／2／3 就是「不要開這個 agent」。
- **內容**：harness **原樣 byte-for-byte**（和 `show` 印的同一份位元組，含 frontmatter）；給 `--append` 時是 **harness 後面接環境說明**，順序固定。**兩份都不整理**：不補空行、不去尾端換行、不動行尾空白、CRLF 照樣留著——要在自己前面留空白的環境說明自己在開頭留。
- **只寫 `--out`**：`--out` 的父目錄是啟動方為那個 agent 準備的，**必須已經存在**；不存在＝exit 2，**不會**替你把目錄樹長出來。除 `--out` 以外不寫任何檔案：不碰 harness 樹、使用者層級指示檔、工作目錄。
- **原子寫入**：同目錄暫存檔＋`os.replace`（中斷或寫不進去只會留下舊檔，不會半寫，也不留暫存檔；`--out` 是 symlink 時換掉的是 symlink 本身、不寫穿到目標）。
- **不指定版本＝版本號最大**；讀取走 `show`／`install` 同一套讀取層（本機複本的 `git show origin/main:<路徑>`，不讀工作樹）。
- **harness 格式先驗**：render 驗 harness 的格式（frontmatter 可解析、`type: Harness`，與 install 走同一條先驗）；格式壞（缺 frontmatter、無法解析、`type` 不是 `Harness`）＝exit 2、`--out` 不會被建立、訊息不含內文。
- 退出碼：**0** 成功；**1** 用法錯（含 `--out ''`、壞 id 格式）、**`--append` 的檔不存在**（含 `--append ''`，且 `--out` **不會被建立**，也不會為此去 fetch）；**2** 複本或讀取失敗、harness 格式壞、`--append` 讀不到（那是檔案問題不是寫錯路徑）、寫不進去 `--out`；**3** harness 查無 id／版本（`HarnessNotFound`，讓呼叫方分辨「查無」與「用法錯」，`--out` 不會被建立）。退出碼優先序：`--append` 先讀，所以讀不到的 append 在未知 id 之前就 exit 2／不存在 exit 1。成功時 stdout 一行：`rendered atelier:worker-team-pm@v1 -> <路徑>`。

## 已知限制

- install 完成前就開的 agent 不會有 harness，要重開一次（worker 開機時 `start.d/20-atelier` 在 angareion-setup 之後才裝）。
- 實機載入只驗過 new 模式（MyLinuxPool#114，2026-10-17）；`--resume`／continue 接續是否載入沒驗。
- 給了 `--harness-file` 時 opencode 的 `OPENCODE_CONFIG_CONTENT` 會取代使用者環境裡同名變數（不是合併）；相對路徑或不存在的路徑會讓 opencode 卡住，所以一律用絕對路徑且先確認存在。
