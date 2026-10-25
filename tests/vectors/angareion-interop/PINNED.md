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
