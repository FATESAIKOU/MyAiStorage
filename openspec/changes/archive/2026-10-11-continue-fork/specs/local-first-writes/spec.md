## MODIFIED Requirements

### Requirement: 只有自己驗過的版本離開 outbox
一筆 outbox 項目 MUST 只在「outbox 裡**現在**的 `session.md`，md5 等於 Drive 上的、也等於這一輪上傳的那一個」時才被移除。上傳期間被新版本取代的，MUST 留在 outbox，等下一輪再傳。

上傳一筆**更新既有 id** 的項目之前，MUST 用這一輪列檔的結果確認那個 id 還在 Drive 上。「更新既有 id」在前景寫入的當下決定：continue 寫回、edit、import 的原地更新、中斷接續的收尾寫回是；新建的 import、merge、fork、另存的 Session 不是。

**寫回一個還沒被 Drive 確認過的 Session 不算更新既有 id**：同一個 id 在 outbox 裡還有一筆新建的項目（沒有標成更新、也沒有上傳程序正在比對它）時，這次寫入 MUST 維持「新建」，上傳時 MUST NOT 因為 Drive 上沒有它而另存。

不在 Drive 上的話（別台刪掉了），MUST NOT 把它傳回去，而是**另存成一個新的 Session**：新的 id，標頭的 parents 指向原本的 id。relation：continue 寫回的另存成 `continue`（和 `session-sync` 的「接續到一半被別台刪掉的」相同）；edit 與其他寫回的沿用原本的 relation（`edit` 不是 relation 的值）。原本的 id 維持被刪的狀態。這和 `session-sync`「接續到一半被別台刪掉的」是同一個規則（使用者 10-03 決定）。

#### Scenario: 別台在上傳前刪掉了
- **WHEN** X 的新版本在 outbox，背景上傳之前另一台機器刪掉了 X
- **THEN** X 沒有被傳回 Drive；這次的修改另存成新的 Session Y（parents 指向 X），提醒說明 X 已被別台刪除、修改存成了 Y

#### Scenario: 還沒上傳就寫回
- **WHEN** 離線時 import X（X 還在 outbox），接著 continue 或 edit X，之後恢復連線
- **THEN** Drive 上出現的是 X 本身（最新的內容），沒有另存，也沒有「X 已被別台刪除」的提醒
