# Atelier 複製外部 skill，是 MyBrain 規則五的有意識例外

> 2026-09-26 re-scope：決定本身不變。文中的分期（期 1／期 2／期 3）、角色（員工、秘書）與範圍（大檔、Atelier）說法，以 `openspec/changes/establish-aistorage-phase1/proposal.md` 與 `docs/backlog.md` 為準。

第三方 skill（例如 mattpocock 的 skill），以及屬於別的系統的 skill（例如 MyBrain 的 `mybrain-*`），都複製一份進 Atelier 成為外部副本，讓員工在執行期只依賴 Atelier、不去外部取。這與 MyBrain 規則五「只用 URL 參照、不複製原文」以及「規則住在被操作的系統裡」方向相反，是有意識的取捨：員工啟動時能載入什麼，不應該取決於外部來源當下是否可達、有沒有被改動。副本會過期的代價（2026-09-09 憑記憶寫的規則副本連踩三輪 CI 紅燈）用兩件事承擔：每份副本都記下出處與版本；定期拿內容跟上游比對，有落差就提出更新、上游消失就提出汰換，更新要過 judge 驗證才套用（期 1 手動觸發，期 2 自動）。
