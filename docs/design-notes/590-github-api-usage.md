# #590 GitHub API 呼叫量測

這是離線假回應下的請求數，不是真實額度消耗或現場流量。執行 `uv run python tests/perf/measure_github_api_calls.py`；計數 transport 不連網、不讀 token。單頻道、單 issue、正常成功、無 rollover；CLI 初始化與設定庫的 git fetch 不在量測中。`GET comments` 是 issue comments API，`GET issue` 是 issue 詳情。

| 操作 | 請求數及類型 |
|---|---|
| send 單行／多行 | 各 2：GET issue + POST comments（PageWriter 先查頁面數量；多行仍一則） |
| read 無新訊息／有新訊息／since | 各 1 GET comments |
| read 跨兩個 HTTP 分頁 | 2 GET comments |
| wait 首輪空／下一輪 304 | 各 1 GET comments |
| inbox 單頻道 | 1 GET comments |
| log 單頻道冷／暖快取 | 各 1 GET comments |
| decision list 單頻道冷／暖 | 各 1 GET comments |
| channel create 正常流程 | 3：POST issues + GET issue + POST registry comments |
| channel list 本地 resolver | 0；不含設定庫 git fetch |

read、inbox、log 和 decision list 對每個選取的頻道和須讀的舊 issue 頁面各查詢；每頁 comments API 最多 100 筆，`Link: next` 會多一個 GET。wait 每 15 秒一輪，無新訊息時逐頻道檢查；找到訊息會提早結束。read 的 `--since` 限制時間窗但不保證只有一頁。wait 只有在單一、未滿的 listing 沒新訊息且有 ETag 時保留驗證器；多頁或滿頁不依賴首分頁 ETag。log 使用獨立鏡像與條件請求。304 仍是一個 HTTP 請求；GitHub 文件所述成功認證的條件 GET 回 304 不計入 primary rate limit，但是否符合條件須依實際認證、回應及額度標頭判斷，不能將所有 GET 都當免費。此探針沒有量到實際額度標頭或 secondary limit。

`aicomm send` 先執行 angareion send，成功後對收件人呼叫 sidecar poke；wake 本身不是 GitHub API。排程喚醒亦走 sidecar；`pmo tick` 不等於喚醒：tick 的 `step_read_round` 執行一次 angareion read，可能另處理訊息及觸發後續動作，整輪額外呼叫依狀態而變。decision ask/update/withdraw 先同步 log、寫訊息，再同步以判斷是否建立 snapshot；submit 和 snapshot 因資料與批次而變，未實測完整流程。

## 情境估算

假設每次喚醒後 inbox 一次，每次回報 send 一次；每次 inbox 涵蓋 C 個單頁頻道，send 為 2 次，排程 poke 本身 0 次。每小時 W 次喚醒、S 次回報：**W × C + 2 × S 次／小時**。例如 C=3、W=4、S=4：20 次／小時，占 5000 的 0.4%；W=12、S=12：60 次／小時，占 1.2%。這些都是請求數上界式近似，不等於扣額數，且不含 wait、tick 額外動作、log 或翻頁。

九隊每天每隊假設 20–100 次 inbox、20–100 次 send，均為三頻道、單頁：9 × (20 × 3 + 20 × 2) = **900 次／日**，至 9 × (100 × 3 + 100 × 2) = **4500 次／日**。平均分布在 8 小時分別為 112.5–562.5 次／小時，即 5000/h 的 2.25%–11.25%；實際尖峰可能高得多，日總量無法推回尖峰。這是全隊合計，避免再重複加上 PM。

如果另有 N 個單頻道 wait 持續空輪詢，15 秒一輪產生 240N 次請求／小時；穩定認證 304 的 primary 額度影響可能較小。每多一個頻道或實際 HTTP 分頁會增加請求；翻頁、舊 issue、錯誤重試及 rollover 可提高數字。

## 未知及限制

- 全部頻道數、頻率、尖峰集中度、304 比率、實際額度標頭皆未知，沒有讀現場資料或憑證。
- `gh pr`／`gh run`、pool-sync、建刪 worker 檢查的頻率、分頁、使用者與 API 路徑未量測；無法估數。git fetch 與 API 配額也不能直接等同。
- tick 的完整一輪、decision 寫入／snapshot、rollover／重試、retired issue、真實 wake 端點皆未以探針跑；上述相關結論是程式碼推論，不是實測總數。
- channel create 的 landing poll 用本地假 store 直接成功；真實設定同步可能有額外 git 流量或等待。
