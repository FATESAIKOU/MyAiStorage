# 授權依 profile，職務不參與授權

執行體（worker、手機 App、同步程式）只負責向儲存要素的介面證明自己是誰；它屬於哪個 profile 由 MyLinuxPool 證明；各要素依 profile 查自己持有的授權規則，並把產生者蓋進 metadata。職務雖然決定員工「該做什麼」，但它是 AI 在執行期自己從 Atelier 載入的，無法被證明，拿來授權就等於讓 AI 自己決定權限。profile 則在 worker 建立時就固定，AI 改不了。員工能跑任意 shell，所以它看得到自己的憑證。這條邊界誠實的講法是：「那個 profile 被允許的操作」就是員工權限的上限，刪掉 worker 就等於撤銷它的身分。

## Considered Options

- 依個別 worker 認證：worker 是用完就丟的，身分跟著生滅，授權規則沒有穩定的對象。
- 所有住民共用一個身分：分不出是誰寫的，也無法單獨撤銷，產生者欄位就失去信任層級的意義。

## Consequences

- 使用者不打算付費升級 GitHub，而免費方案的 private repo 無法用原生權限切開「能開 PR」和「能 merge」、「能推分支」和「能推 main」。所以期 1 員工對 MyBrain 只讀；員工寫入 MyBrain 要等基本設計找到不靠付費方案的做法（期 2）。
- 實作優先用 MLP 現有的 profile secrets 注入，以及儲存實體原生的權限與版本功能，盡量不新增常駐的代理服務。
