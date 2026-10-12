# config/angareion

Angareion 的設定檔。兩個檔都是 UTF-8、2 格縮排、key 排序、檔尾換行（diff 整齊）；都有 `version` 欄（目前 `1`）。

## backends.json（走 PR）

backend 本身的設定。更新走 PR（AiStorage PM 審查＋merge，d 等級）；angareion 只從這個檔知道 backend 的位置，個人設定檔裡不放 backend 的位置。

```json
{
  "backends": {
    "github-main": {"repo": "owner/repo", "type": "github"}
  },
  "version": 1
}
```

- `type` 目前只有 `github`（未知 type＝設定錯誤，angareion exit 2）。
- GitHub backend 的 `repo` 是放實體的 repo（`owner/repo`）。

目前 repo 裡的 `backends` 是空的：等各專案用 d 等級 PR 把自己要用的 backend 加進來。

## channels.json（機器產物，不要手改）

channel 名 → backend 實體的對應。由 MyAiStorage 的**註冊簿 issue** 上的留言，經 `.github/workflows/angareion-registry.yml` 重建後直接 commit 進 main。

```json
{
  "channels": {
    "general": {"backend": "github-main", "issue": 123}
  },
  "version": 1
}
```

- 這個檔是重建的輸出：人工改會被下一次重建還原，**不要手改**。
- 名字不在檔裡＝channel 不存在（`send`／`read`／`wait`／`channel delete` exit 1）。
- 註冊簿與留言格式見 `registry-issue.md`。
