# impl2：`agora-opencode load` 之後 assistant 回合不見了（9.1／9.2 的根因）

- 執行時間：2026-09-30 UTC；執行者：impl2
- 環境：`aistorage-resident:latest`（ubuntu 24.04、Python 3.12.3），`opencode 1.18.32`
- 隔離：自己起的 probe 容器（`impl2-probe`／`aistorage-it-load-<pid>`），**沒有**動
  e2e 的容器池、`resident_pool`、git-annex 或任何 Drive 憑證；沒有用到任何模型
  （送模型的請求由本機 stub provider 接），不需要網路。
- 重現的輸入：9.1 失敗那輪留在
  `~/.local/share/aistorage/work/e2e/e2e-s2/pkg-s2` 的起點包（**唯讀**取用，沒有改它）。
- 判定：**根因是轉接器自己重編的 id 互相重複**，`opencode import` 把它們靜默丟棄。
  已修（`src/aistorage/adapters/opencode/loader.py`），並補上整合測試當守門。

## 1. 症狀（impl3 在 9.1／9.2 實跑發現）

- 9.1：轉接器交給 `opencode import` 的內容 3 則（user、帶 tool 的 assistant、純文字
  assistant）；import 之後再 export 只剩 **1 則 user**，3407 bytes（應該約 72 KB）。
- 9.2：8 則（去掉 reasoning part）import 之後 export 出來是 **0 則**。

## 2. 原因：id 被截斷到**互相重複**，`import` 靜默丟棄

`import` 的實作（1.18.32，由 `/usr/local/bin/opencode` 裡的原始碼確認）：

```js
for (let z of B.messages) {
  let X = decodeUnknownSync(Message.Info)(z.info)          // 形狀錯 → 直接丟錯
  yield* db.insert(message).values({id, session_id, time_created, data})
    .onConflictDoNothing().run()                          // id 撞 → 靜默丟棄
  for (let _ of z.parts) {
    yield* db.insert(part).values({id, message_id, session_id, data})
      .onConflictDoNothing().run()                        // 同上
  }
}
```

所以有兩種失敗：**形狀不對會大聲報錯，id 重複會完全沒聲音**（rc=0、沒有訊息）。

而當時的轉接器是這樣編 id 的（`loader.py`）：

```python
new = f"msg_{tag}{i:020d}"[:30]          # tag = 預留 session id 的尾 10 碼 + 段落標記
p["id"] = f"prt_{tag}{i:010d}{j:06d}"[:30]
```

`"msg_"`（4）＋ tag（最多 12）＋ 序號（20 進位補 20 碼）＝ 36 個字元，`[:30]` 把
**序號的位數全部吃掉**：

```
msg_PC0ZPQJSHP0X00000000000000   ← 8 則訊息全部是這一個 id
msg_PC0ZPQJSHP0X00000000000000
…
```

`time.created` 相同時被拿來當排序鍵的整數序號，兩邊都落在截斷線之外。8 則訊息、
27 個 part 匯入之後只留下 1 個 message row 與 8 個 part row（part 的 id 也一樣被
截斷：同一則訊息內 4 個 part 拿到同一個 id），export 出來就是 **1 則**。

9.2 的 **0 則**是同一個 bug 的另一副面孔：那次是同一個起點包 `load` **第二次**，
全部 id 撞上第一次匯入的那批（id 是 DB 的全域主鍵，跨 session 也算），於是整批被丟掉。
9.1 與 9.2 因此是同一個根因的兩種暴露方式。

容器裡重現（`pkg-s2` 那個起點包，8 則訊息）：

```
$ agora-opencode load /work/pkg-s2 -C /work     # 舊的 loader
ses_0ACH1MPC0ZPQJSHP
$ opencode export ses_0ACH1MPC0ZPQJSHP > o.json
3407 o.json        →  messages: 1  [(msg_PC0ZPQJSHP0X00000000000000, user, 8)]
```

## 3. 順手量到的 id 規則（review 當時問過、spike 沒測的）

在容器裡直接對 `opencode import` 做各種形狀（每個都是乾淨的資料庫）：

| 匯入的 id | 結果 |
|---|---|
| `msg_01a0ee3e449d0000009479fc`（本文件的新形狀） | 收；9／9 則、31／31 個 part，內容與順序與來源相同 |
| `00000000000000000000`（**以數字開頭**、沒有前綴） | **拒絕**：`Expected a string starting with "msg", got "00000000000000000000"` |
| `msg_ZZZ…ZZZ000`（66 個字元） | 收（**沒有長度上限**） |
| `msg.a-b_c/0?`（含 `.` `-` `_` `/` `?`） | 收（字元集不限制） |
| 全部訊息同一個 id | 收，**靜默丟棄**：9 則進、1 則出 |
| part id 遞減（前綴正確） | 收，但**訊息內 part 的順序被翻轉** |

結論三條，都寫進 `loader.py` 的註解：

1. **前綴一定要有**（`ses_`／`msg_`／`prt_`），否則整份匯入被拒絕——不是靜默丟棄。
2. **id 必須唯一**，而且是 DB 的**全域**主鍵：不同 session、不同匯入也不能重複。
3. **id 的字典序要跟匯出順序一致**。匯出的排序是
   `ORDER BY time_created, id`（訊息）與 `ORDER BY message_id, id`（同一則訊息內的
   part）。時間不同的時候順序由 `time_created` 決定；**時間相同（合成資料、同毫秒
   產生）時由 id 決定**——實測把訊息 id 弄成遞減，匯出順序就整個翻轉。

## 4. 修法

`loader.py` 的重編 id 改成三段固定寬度，**沒有任何可以被截斷的地方**：

```
msg_ / prt_ + <time.created 的 12 碼 16 進位> + <序號 6 碼> + <鹽雜湊 6 碼>   = 28 字元
```

- **時間**：原始紀錄的 `time.created`。id 的字典序因此與時序一致（opencode 自己就是
  「時間＋亂數」的形式），第 3 點的排序問題一起解決。
- **序號**：這次匯入裡的位置（n→1 時跨段遞增），負責唯一。
- **鹽雜湊**：由**預留 session id** 推導（`salt_for`，不是隨機）。所以
  - 1→n：每份起點包的預留 id 不同 → 鹽不同 → id 不同；
  - **重跑同一個起點包：鹽相同 → id 相同 → 匯入是 no-op**，不會把歷史複製一份，
    也不會變成 9.2 的 0 則。
- 超出 30 個字元時**明確報錯**，不再截斷（`pragma: no cover`：固定寬度加起來是 28）。

## 5. 驗證

`tests/integration/test_opencode_load_roundtrip.py`（`pytest -m integration`，**不需要
模型、Drive 或網路**）：真的 resident 容器 → 造形狀真實的匯出檔 → `import` → `export`
取得原始紀錄 → 用 repo 自己的 `write_package` 組起點包 → `agora-opencode load` →
再 `export`，然後斷言訊息數／part 數／內容／順序與原始紀錄一致、id 唯一且遞增、
重跑不變、1→n 兩份完整，再用 spike 的 stub provider 抓**真的** request body 比對
（送給模型的共同前綴位元組相同、1→n FULL 位元組相同）。

把 `loader.py` 暫時換回舊的 id 寫法，這支測試立刻用 9.1／9.2 的形狀失敗：

```
E  AssertionError: {'messages': 1, 'parts': 1, 'bytes': 1543, ...}     # 9.1：1 則
E  AssertionError: {'messages': 0, 'parts': 0, 'same_shape': False}    # 9.2：0 則
E  AssertionError: 1→n 的兩份 request body 不同：[32329, 32237]
```

## 6. 順便量到、但**不屬於這次修法範圍**的兩件事

驗 n→1（兩個片段合成一個匯出）時用 8 則訊息實測，兩件事都跟 id 重編無關、修法前後
一模一樣，所以這裡只記下來，不動程式：

1. **匯出順序由 `time_created` 決定，所以片段的時間不能交錯**。兩個片段的
   `time.created` 有重疊時，匯出會把兩段交錯著排出來（實測：段 1 五則、段 2 三則，
   匯出順序變成 1,2,1,2,1,2,1,1）。ADR 0010 要求「最長的一段放最前面」是對**匯出
   檔的陣列**而言的，opencode 匯入後會用自己的排序再排一次。時間不交錯的片段
   （spike 的 SEED＋SMOKE 就是這樣）沒有這個問題。要不要在 `checkout` 端主動檢查
   片段之間的時間是否交錯，是 n→1 的決定範圍，先不動。
2. **`import` 會把 user 訊息的 `parentID` 丟掉**。真實的 opencode 匯出檔裡，
   **user 訊息根本沒有 `parentID` 這個欄位**（只有 assistant 有），而 zod 解析會
   去掉 schema 沒宣告的欄位——所以 `chain_parents` 手工鏈的那條 link，若該則訊息
   是 user 訊息，匯入後在 DB 裡就不見了（實測：`select json_extract(data,
   '$.parentID')` 是空的）。n→1 的第一則通常是 user 訊息，也就是說這條鏈接在常見
   情況下沒生效。影響多大（重播是照陣列順序，理論上不受影響；受影響的是
   rewind／分支的樹狀結構）要另外驗，屬於 n→1 的議題。

## 7. 這個 bug 為什麼會活到 e2e

轉接器的 id 寫法是**從 spike 的腳本逐字搬過來的**
（`scripts/spike/session_import_make.py`，當時 `--tag IMPA`，4 個字元）：

```python
new = f"msg_{tag}{i:020d}"[:30]     # 4 + 4 + 20 = 28 ≤ 30，沒有被截斷
```

spike 用 4 碼 tag，總長 28，**剛好在門檻內**，所以 4 則、2 則、8 則的匯入全部正常，
而且 spike 從頭到尾只驗「訊息數」與「送模型的位元組」——兩者都對重複的 id 是
**不敏感**的（`onConflictDoNothing` 丟掉重複的之後，剩下的剛好就是第一則，所以
「第一則 user 訊息還在」看起來完全正常）。真正換掉 tag 長度的是
`agora-opencode` 的實作：它用**預留 session id 的尾 10 碼**再加段落標記當 tag，
長度從 4 變成 12，於是 `[:30]` 開始吃序號。

換句話說：spike 驗的是「這個長度的 tag」，實作驗的是「任何長度」；兩者差一格就
整批不見，而且不報錯。教訓是**寫 id 的地方不該有截斷**——寧可長度不對時丟錯。
現在的整合測試用三種長度的預留 session id 走過 `build_export`，並且在容器裡真的
匯入再匯出，兩道都擋得住。
