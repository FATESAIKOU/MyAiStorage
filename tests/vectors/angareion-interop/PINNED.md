# 釘住的來源

這個目錄是從 FATESAIKOU/MyAiEntry 原樣複製來的（不改複製來的檔；`SOURCE.md` 是來源那邊的說明，裡面的「待辦」以來源當時為準）。

- 來源 repo：FATESAIKOU/MyAiEntry
- 來源 commit：`801013c`
- 來源路徑：`test/fixtures/angareion-interop/`
- 測試：`tests/unit/test_angareion_interop.py`

## 重新取得

```
git -C <MyAiEntry 的 clone> archive 801013c test/fixtures/angareion-interop | tar -x -C <暫存目錄>
```

再把 `<暫存目錄>/test/fixtures/angareion-interop/` 的內容原樣複製到這裡，並把上面的 commit 改成新的。

`python-written/` 與 `expected.json` 由 `generate_python_written.py` 重產（用 MyAiStorage 的 `serialize_message`、`recipient_matches`、`mention_matches`），做法見 `SOURCE.md`；`ts-written/` 由 MyAiEntry 的 TS 測試重產。重產後必須重跑 `tests/unit/test_angareion_interop.py`。

`python-written-py-cannot-read/` 的兩則（10、11）在 `split_front_matter` 修好（#231）之前 `parse_message` 讀不回；修好後由測試覆蓋。

`python-written-py-cannot-read/` 併進主資料要兩個 repo 一起做（另開單），不要單邊併。

## 本地新增（不是來源的檔）

`local-additions/` 裡的檔是這個 repo 自己加的，**不是**從 MyAiEntry 複製來的（#232）：

- `99-crlf-content.md`：整份訊息（front matter 與 content）的行尾都是 `\r\n`，用位元組寫成。
- `99-crlf-content.expected.json`：預期讀出的欄位；content 是 `body\nmore\n`（`\r\n` 讀成 `\n`，同 TS 的 `splitFrontMatter`）。
- `98-lone-cr-front-matter.md`：front matter 的兩個欄位之間是**單獨的 `\r`**（不是 `\r\n`），`urgency: 5\rattachments: []`，用位元組寫成。
- `98-lone-cr-front-matter.expected.json`：預期讀出的欄位；`urgency` 是 5、`attachments` 是空的（YAML 把單獨的 `\r` 當換行，兩個欄位讀得開）。
- `98-lone-cr-in-quoted-string.md`：雙引號字串裡有**生的 `\r`**（`name: "a\rb.md"`），其餘行尾是 `\n`。
- `98-lone-cr-in-quoted-string.expected.json`：預期讀出的欄位；附件名是 `a b.md`（YAML 把雙引號內的換行折成空白，單獨的 `\r` 是換行）。

98 的兩則是 #238 的；兩個例子查的是不同的讀法（欄位分隔、雙引號內折行），所以拆成兩則，一則壞掉時看得出是哪一種。

#238 已定案：YAML 規格把單獨的 `\r` 當換行，**PyYAML 的讀法對，TS 讓步**。這裡不改讀法；MyAiEntry 的 TS 讀法要改成跟這裡一致（另開單，補完之後下次釘新 sha 時把 98 的兩則一起併過去）。

MyAiEntry 在 TS 那邊補上 `local-additions/` 裡的這些檔之後，下次釘新 sha 時要把這些檔併過去（併進 `ts-written/`／`expected.json`，再刪掉這裡的檔）。
