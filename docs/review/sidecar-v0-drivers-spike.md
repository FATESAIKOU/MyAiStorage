# sidecar-v0 驅動程式 spike：兩個 driver 的 `env_for` 在真的 agent 上對不對（S6）

- 日期：2026-10-12，架構師（Claude Sonnet 5.5）。
- 對象：`sidecar-v0-agy4` 的 `80966b0`（3.2 `OpenCodeDriver`）與 `c95d268`（3.1 `ClaudeCodeDriver`）；`git archive` 到 `/tmp/s31c`，只用 `PYTHONPATH` 取它的 `env_for` 與 `subproc.child_env` 的輸出，沒有改 impl3 的任何檔。
- 版本：opencode 1.18.35、Claude Code 2.1.296（本機）。

## 方法（全部在本機 loopback）

- **假 provider**（`127.0.0.1:<埠>`，Python 標準函式庫）：`/v1/chat/completions`（OpenAI 相容，含 streaming）與 `/v1/messages`（Anthropic，含 SSE streaming）都回一則最小的正常回覆。**只記錄 header 的「名稱」，以及 `Authorization`／`x-api-key`「是否等於」假 key（布林）、「是否等於」父行程的假值（布林）、請求本文裡的 `model`**；不記錄任何 header 值。
- **假 subscription**：key 是 `FAKE-SUBSCRIPTION-KEY-0001`，endpoint 指到假 provider；用副本的 `env_for` 與 `child_env` 組出子行程環境。
- **隔離**：`HOME`、`XDG_CONFIG_HOME`、`XDG_DATA_HOME`、`XDG_STATE_HOME`、`XDG_CACHE_HOME` 全部指到 `/tmp` 下的新目錄；父行程先清掉 `ANTHROPIC_*`、`OPENAI_*`、`OPENROUTER_*`、`OPENCODE_*`、`CLAUDE_*`，再故意設 `ANTHROPIC_API_KEY=parent-should-not-leak`、`OPENAI_API_KEY=parent-should-not-leak`。
- **對外流量**：Claude 設 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`、`DISABLE_AUTOUPDATER=1`；opencode 設 `OPENCODE_DISABLE_AUTOUPDATE=1`、`OPENCODE_DISABLE_MODELS_FETCH=1`（這兩個名字都在 binary 裡找到）。模型請求全部落在假 provider。
- 沒有讀 Keychain、沒有讀真的 `~/.claude`、`~/.config/opencode`、`~/.local/share/opencode`；沒有用任何真的 key。

## 結論

| agent | `env_for` 現況 | 結論 |
|---|---|---|
| opencode | key 與 baseURL 經 `OPENCODE_CONFIG_CONTENT` **有效**（假 provider 看到的 `Authorization` 等於假 key，不等於父行程的值） | **機制對，但輸出不完整，會在真環境失敗或打到錯的模型（3 個問題，見下）** |
| Claude Code | `ANTHROPIC_API_KEY`／`CLAUDE_CODE_OAUTH_TOKEN` 都有效；子行程沒拿到父行程的 `ANTHROPIC_API_KEY` | **機制對；對「非 Anthropic 的 api-key（例如 OpenRouter）」與「指定模型」兩處要補** |

兩邊的「父行程的憑證不外漏」都成立：`child_env` 的允許清單把 `ANTHROPIC_*`、`OPENAI_*`、`OPENCODE_*` 全部擋掉，假 provider 看到的值「等於 subscription 的假 key」為真、「等於父行程的值」為假。

## opencode

### 實測

| 設定（`OPENCODE_CONFIG_CONTENT`） | 結果 |
|---|---|
| impl3 的形狀：`{"provider": {"openrouter": {"options": {"apiKey", "baseURL"}}}}`（沒有 `model`） | 請求到了假 provider：`POST /v1/chat/completions`，key 等於為真、父行程為假；但**兩次請求的模型是 opencode 自己挑的**：主要的是 `google/gemini-3-pro-image-preview`（一個圖像預覽模型，不是 subscription 指定的），標題用的 small model 是 `google/gemini-3.8-flash` |
| 加 `"model": "openrouter/acme/spike-model"`、`"small_model"` 同一個，**但沒有宣告這個模型** | `POST /session/{id}/message` 回 **HTTP 500**，假 provider 什麼都沒收到（opencode 找不到這個模型） |
| 同上，並在 `provider.openrouter.models` 宣告 `"acme/spike-model": {"name": "spike"}` | **通**：兩次請求（主模型＋標題，都用同一個 small_model）都是 `acme/spike-model`，key 等於為真；回應的 `info.providerID`／`modelID` 可以拿來核對 |
| provider 用 LLMGateway 的名稱 `opencode-zen` | **500**，什麼都沒送出（opencode 的 Zen 的 provider id 是 `opencode`，不是 `opencode-zen`） |
| provider 用 `opencode`，`"model": "opencode/big-pickle"` | **通**，key 等於為真 |
| `auth.json`（已儲存的登入）有另一把 key，`OPENCODE_CONFIG_CONTENT` 又給一把 | 假 provider 看到的是 **config 裡的那把**（config 優先於已儲存的登入）；`OPENCODE_AUTH_CONTENT` 不需要 |

### 最小可用的 JSON 形狀（不含真 key）

```json
{
  "provider": {
    "openrouter": {
      "options": {"apiKey": "<key>", "baseURL": "https://…/v1"},
      "models": {"<model-id>": {"name": "<model-id>"}}
    }
  },
  "model": "openrouter/<model-id>",
  "small_model": "openrouter/<model-id>"
}
```

- `provider` 的鍵是 **opencode 的 provider id**；`models` 的宣告讓不在 opencode 內建登錄的模型也能用（內建的有就不用宣告，但宣告無害）；`model` 與 `small_model` 的格式是 `<providerID>/<modelID>`。
- `OPENCODE_DISABLE_MODELS_FETCH=1`（不抓模型清單）與 `OPENCODE_DISABLE_AUTOUPDATE=1` 實測有效；子行程只會有 `env_for` 給的 `OPENCODE_*`，所以這兩個要由 driver 自己放進環境（`child_env` 會把父行程的 `OPENCODE_*` 全部擋掉）。

### 要改的（給 impl3）

1. **provider id 的對應**（會直接失敗）：`env_for` 現在用 `subscription.provider` 當 `provider` 的鍵。LLMGateway 的共通 JSON 裡 Zen 叫 `opencode-zen`，opencode 要的是 `opencode`。在 driver 裡放一張對應表：`opencode-zen → opencode`、`openrouter → openrouter`、`ollama-cloud → ollama-cloud`（最後一個我沒有在真 binary 上驗，只確認 `openrouter`、`opencode` 兩個）。對應不到的直接丟錯，不要默默用原名。
2. **一定要設 `model` 與 `small_model`**（會打到錯的模型）：來源是 `subscription.models.default`；沒有 default 時，用 `models.allow` 裡第一個不含萬用字元的；兩個都沒有就拒絕（`env_for` 丟錯，open 回 4xx），不要讓 opencode 自己挑。opencode 在隔離環境挑了一個圖像預覽模型，在真環境可能是任何東西（甚至是 Zen 的公開模型）。
3. **宣告 `provider.<id>.models`**（不宣告會 500）：把上一點選出的模型 id 放進去。
4. 順手：driver 把 `OPENCODE_DISABLE_AUTOUPDATE=1`、`OPENCODE_DISABLE_MODELS_FETCH=1` 加進環境；`launch` 後第一次回應可以核對 `info.providerID`／`modelID` 是不是 subscription 指定的（不是就當錯誤）。

## Claude Code

### 實測（`claude -p --output-format stream-json --verbose "hi"`，隔離 HOME，不 resume）

| `env_for` 的輸出 | 假 provider 看到的 | 備註 |
|---|---|---|
| `kind=api-key`：`ANTHROPIC_API_KEY`＋`ANTHROPIC_BASE_URL` | `POST /v1/messages`，`x-api-key` 等於假 key 為真、父行程為假；沒有 `Authorization` | 事件序列 `system:init` → `assistant` → `result:success`，**沒有 `rate_limit_event`**（和 D6「api-key 類沒有這個事件」一致）；模型是 Claude 預設的 `claude-opus-5-5` |
| `kind=claude-subscription`：`CLAUDE_CODE_OAUTH_TOKEN`＋（測試用的）`ANTHROPIC_BASE_URL` | `Authorization` 等於假 key 為真；沒有 `x-api-key` | 有 `rate_limit_event`；預設模型 `claude-sonnet-5-5` |
| `ANTHROPIC_BASE_URL`＋`ANTHROPIC_AUTH_TOKEN`＋`ANTHROPIC_API_KEY=""`（LLMGateway design 決定 7 對 OpenRouter 的寫法） | `Authorization` 等於假 key 為真；**沒有 `x-api-key`** | 驗證 Claude 在這組環境下只送 Bearer |
| `ANTHROPIC_API_KEY`＋`ANTHROPIC_MODEL=acme/spike-model`＋`ANTHROPIC_SMALL_FAST_MODEL` 同值 | 請求本文的 `model` 是 `acme/spike-model` | `ANTHROPIC_MODEL` 有效 |

沒有出現「請求沒到假 provider」或「key 相等為假」的情況，所以沒有觸發停下的條件；我也沒有去查 Keychain。**隔離 HOME 並不隔離 macOS 的 Keychain**，這台機器若有登入，上面的結果表示環境變數的 key 勝出；這台有沒有登入我不知道，所以這不是對「Keychain 優先序」的證明（見下面的「沒驗到的」）。

### 要改的（給 impl3）

1. **非 Anthropic 的 api-key（例如 OpenRouter）用 `ANTHROPIC_AUTH_TOKEN`＋`ANTHROPIC_API_KEY=""`，不是 `ANTHROPIC_API_KEY`**：LLMGateway 的設計（決定 7）就是這樣定的；impl3 現在對所有 `kind=api-key` 一律給 `ANTHROPIC_API_KEY`，會送 `x-api-key`，而這類 Anthropic 相容端點是收 `Authorization: Bearer` 的（我無法離線驗證 OpenRouter 本身接受哪個，依據是 LLMGateway 設計的敘述與我驗證的 Claude 行為）。建議：`provider == "anthropic"` 且 `kind == "api-key"` 才用 `ANTHROPIC_API_KEY`；其他 provider 用 `ANTHROPIC_AUTH_TOKEN`＋`ANTHROPIC_API_KEY=""`。（`env_for` 回傳空字串的 `ANTHROPIC_API_KEY` 沒有問題，`child_env` 只是把 `env` 合併進去。）
2. **指定模型**：`subscription.models.default` 有值時設 `ANTHROPIC_MODEL`，同時設 `ANTHROPIC_SMALL_FAST_MODEL`（避免標題等次要請求去用 Claude 預設的 small model 而打到第三方不認得的名字）；沒有 default 的 Anthropic 訂閱不設，讓 Claude 用自己的預設。我只驗了 `ANTHROPIC_MODEL` 改變主請求的 `model`；`ANTHROPIC_SMALL_FAST_MODEL` 在這次單輪沒有被用到，沒有實測。
3. 不用改：`CLAUDE_CODE_OAUTH_TOKEN`（訂閱）與 `ANTHROPIC_BASE_URL` 的用法都對；`child_env` 的允許清單確實讓父行程的 `ANTHROPIC_API_KEY` 不進去。

## 沒驗到的與坑

- **真實 HOME 的優先序沒驗**（規則禁止讀 `~/.claude`、Keychain）：官方文件說的優先序是環境變數（`ANTHROPIC_AUTH_TOKEN`／`ANTHROPIC_API_KEY`／`CLAUDE_CODE_OAUTH_TOKEN`）高於 `/login` 存的訂閱；我的結果和它一致，但那是在隔離 HOME。建議 PM 之後在 Mac 上用 3.4 的實機跑時順便確認：真 HOME 下 sidecar 開的 agent，請求確實用 subscription 的 key（可以用同樣的假 provider 與布林記錄）。
- **opencode 與真實 HOME**：全域的 `~/.config/opencode/opencode.json` 會和 `OPENCODE_CONFIG_CONTENT` 合併（後者最後套用，文件如此），已儲存的 `auth.json` 在衝突時輸給 config（我實測了隔離 HOME 的這一項）。真 HOME 下使用者自己的 provider／plugin 設定仍然會被載入，不是乾淨的環境；如果要乾淨，`OPENCODE_PURE=1`、`OPENCODE_DISABLE_PROJECT_CONFIG=1` 可以考慮（我沒有測它們對 sidecar 的影響）。
- **opencode 的 `provider` 即使沒有憑證也會列出 `opencode`（Zen 的公開免費模型）**：`env_for` 回空環境、又沒設 `model`，opencode 可能預設去打真的 Zen。所以「一定設 model」不只是正確性，也是不要打到不該打的地方。
- **500 的訊息很少**：模型找不到與 provider 不存在在 HTTP 上都只是 500，沒有可讀的原因（stderr 的 log 才有）。driver 的錯誤處理要把子行程的 stderr 末幾行放進 log（不含 key）。
- **opencode 每一則訊息會多一次標題請求**（用 small model），不設 `small_model` 就是另一個模型；用額度的 subscription 要把它算進去。
- **對外流量**：關掉自動更新與模型清單之後，opencode 與 Claude 在這些測試裡沒有其他我觀察得到的對外請求（我沒有做封包層級的檢查，只確認模型請求都落在假 provider）。

## 給 impl3 的建議清單（PM 派）

1. `OpenCodeDriver.env_for`：provider id 對應表；`model`／`small_model` 必設（無來源就拒絕）；宣告 `provider.<id>.models`；環境加 `OPENCODE_DISABLE_AUTOUPDATE=1`、`OPENCODE_DISABLE_MODELS_FETCH=1`；首次回應核對 `providerID`／`modelID`。
2. `ClaudeCodeDriver.env_for`：非 anthropic 的 api-key 用 `ANTHROPIC_AUTH_TOKEN`＋`ANTHROPIC_API_KEY=""`；`models.default` 設 `ANTHROPIC_MODEL`＋`ANTHROPIC_SMALL_FAST_MODEL`；環境加 `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`、`DISABLE_AUTOUPDATER=1`。
3. 測試：用我的假 provider 的做法寫成單元測試（loopback，只記布林）放進 `tests/unit/`；opencode 與 claude 都不在 CI 時 `skip`（本機有 binary 才跑）。
4. 把 stderr 末幾行（遮掉 key）放進 driver 的錯誤訊息。
