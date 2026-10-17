# `atelier` 指令（v0 工具；PR 1）

讀 Atelier harness 樹（`atelier/<team>/<role>/v<N>.md`），並把一份 harness 裝進 agent 的使用者層級指示檔。與 `agora`／`angareion` 同一家族（同一個 distribution，`uv tool install` 後就有 `atelier`）；本 PR 只有工具，harness 檔本體是 PR 2。設計全文：`openspec/changes/atelier-v0/`。

## 指令

```
atelier ls [--json]
atelier show <id>[@<版本>] [--section know|do|judge|dont|capabilities] [--json]
atelier install <id>[@<版本>] --agent <claude|opencode> [--agent …]
```

- `<id>` 是 `atelier:<team>-<role>`（例 `atelier:worker-team-pm`）；`@v<N>` 不給＝同一職務檔案裡最大的 N。
- `ls`：一個版本一行 `atelier:worker-team-pm@v0`；`--json` 給 `[{"id","team","role","version"}, …]`（照 team／role／版本排序）。
- `show`：預設整份**原樣 byte-for-byte**（含 frontmatter；就是 `git show` 的位元組，不整理、不加行）；`--section` 只印該章節本文（不含 `##` 標題、去頭尾空行，行內原樣）；`--json` 給 `{"id","team","role","version","frontmatter","sections"}`，跟 `--section` 一起用時給 `{"id","team","role","version","section","content"}`。
- `install`：`--agent` **可以給多次**；每個值各裝一次、與順序無關、同值只裝一次；**不給＝exit 1**。不指定版本＝裝最大 N。
- 退出碼：**0** 成功；**1** 用法錯、id／版本／章節查無（查無會先 fetch 一次）；**2** 複本不存在、git 或檔案問題、install 寫不進去。訊息不含 harness 內文。

## 複本與 fetch（與 angareion 共用）

- 讀取走本機複本（MyAiStorage 的 clone）：`git fetch origin main` 更新、`git show origin/main:<路徑>` 讀檔；**不依賴工作樹**（本機改動、detached HEAD 都無妨）、匿名 HTTPS、不用憑證。
- **複本路徑**：`ATELIER_CONFIG_DIR`；沒設就看 angareion 的 `config.json`（`ANGAREION_CONFIG`，預設 `~/.config/angareion/config.json`）裡的 `config_dir`——兩邊共用同一份複本。
- **同一把 flock**：`<複本>/.git/angareion-pull.lock`（angareion 用的同一個鎖檔，兩邊的 fetch 因此不並行）；git 逾時 **20 秒**。
- fetch 節流：指令開始時距上次 fetch（`FETCH_HEAD` 的 mtime）超過 `config_pull_interval` 才拉——**沿用 angareion `config.json` 的 `config_pull_interval`**（沒設＝15 分鐘／900 秒；`0`＝不按週期拉，查無時的事件驅動 fetch 仍會做）；查無 id／版本時再補一次（距上次不到 30 秒就跳過）。**fetch 失敗只警告一次（stderr）並沿用目前的複本，退出碼不變。**
- 唯一的本機寫入就是複本自身的 `git fetch`（以及它建立的共用鎖檔 `<複本>/.git/angareion-pull.lock`）和 install 的目標檔；工具不會寫 harness 樹、工作目錄或使用者其他檔案。

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
- PR 2 已放第一份職務檔（`atelier/worker-team/pm/v0.md`，識別 `atelier:worker-team-pm@v0`）：真實複本上的 `atelier ls` 會列出它。

## 已知限制

- install 完成前就開的 agent 不會有 harness，要重開一次（worker 開機時 `start.d/20-atelier` 在 angareion-setup 之後才裝）。
- 實機載入只驗過 new 模式（MyLinuxPool#114，2026-10-17）；`--resume`／continue 接續是否載入沒驗。
