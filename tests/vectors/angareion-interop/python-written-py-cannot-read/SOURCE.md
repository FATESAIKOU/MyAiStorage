# python-written-py-cannot-read 來源與用法

MyAiEntry#208 低 2。這兩則是 MyAiStorage `8d97ebc` 的 `serialize_message` 真的寫出來的，但**同一個 commit 的 `parse_message` 讀不回來**（回 `None`）：inline 內文有一行 `---` 時，PyYAML 把它寫成縮排的 `    ---`，而 Python 的 `split_front_matter` 用 `strip()` 後比對，把縮排的 `---` 當成 front matter 的結尾。

## 這是 MyAiStorage 那邊已知的 bug

PMO 已轉。修好之後（`split_front_matter` 只認沒有縮排的 `---`），把這個目錄的 `inputs.json` 併進上一層的 `inputs.json`、重產 `expected.json`（用 Python 真函式），再刪掉這個目錄。在那之前**不要**把這兩則放進共用的 `expected.json`：那份是 Python 真函式算出來的，這兩則算不出來。

## MyAiStorage 要修的兩件

1. `split_front_matter`：結尾 `---` 只認沒有縮排的那一行（行尾容許空白與 `\r`），不要 `strip()` 後比對。
2. `serialize_message`：送出前自己 `parse_message` 一次，讀不回就拒送（這個目錄的兩則就是 `serialize_message` 寫得出、`parse_message` 讀不回的例子）。

## 檔案

| 檔 | 內容 | 誰產生 |
|---|---|---|
| `inputs.json` | 兩則假訊息的欄位 | 手寫 |
| `python-written/*.md` | `serialize_message` 實際輸出 | `generate_python_written.py` |

- 10：inline 內文 `before⏎---⏎after⏎`
- 11：inline 是帶 front matter 的 markdown 文件（`---⏎title: …⏎---⏎…`），content 另有一行 `---`

## 來源

- 來源 repo：FATESAIKOU/MyAiStorage，commit `8d97ebc`（`src/angareion/messages.py` 的 `serialize_message`）
- 重產：`git archive 8d97ebc src | tar -x -C <dir>`，再
  `python3 -I generate_python_written.py <dir>/src <此目錄>`（只呼叫 `serialize_message`，不呼叫 `parse_message`）

## TS 測試

期望值由 `inputs.json` 直接算（name／inline／ref／content 原樣），測試在 `test/unit/infra/angareion/messages.test.ts` 的 describe「python-written-py-cannot-read」。
