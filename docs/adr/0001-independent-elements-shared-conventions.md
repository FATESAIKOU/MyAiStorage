# 四個儲存要素各自獨立，AiStorage 只定共通約定

MyBrain、Atelier、Agora、Foundry 在寫入者、寫入頻率、要不要人 review、資料大小上都不同。例如 Agora 是高頻、自動、GB 級的追加；MyBrain 是低頻、要經本人 review 的文字。依「統一的兩端稅」，這是「不同的事」：用一個統一的 AiStorage 服務或 API 裝它們，兩端會同時適應不良，而且方向相反（一端嫌太重，一端嫌不夠細）。所以每個要素各有自己的儲存實體與介面，AiStorage 只統一共通約定：項目＝metadata＋本體、六個共通 metadata 欄位、互相參照的方式，以及以 profile 為單位的身分。

## Considered Options

- 一個統一的 AiStorage 服務（API／MCP）：AI 只需要認識一個入口，但會把 MyBrain 的 review 流程和 Agora 的自動寫入硬塞進同一套權限與寫入模型。
- 四個要素共用一個儲存實體（同一個 repo 或 bucket）：會讓儲存選型被最嚴格的那個要素綁住，例如影片的大小與 MyBrain 的 PR 流程互相衝突。
