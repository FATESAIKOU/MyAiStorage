# 註冊簿 issue

`channel create`／`channel delete` 在 MyAiStorage 的**註冊簿 issue** 上各留一則留言（一行 JSON）；`.github/workflows/angareion-registry.yml` 讀整串留言重建 `config/angareion/channels.json` 後直接 commit 進 main。

建立這張 issue 是部署工作（第 5 節；由 PM／本人做），內容如下。

## issue

- 標題：`Angareion channel 註冊簿`
- label：`層級:d`、`狀態:進行中`
- 長期存在、**不要關閉**（與一般工單「做完關閉」不同；d 不佔 b／c 的同時進行額度，有 label 不會被列「樹外」）。
- 置頂留言：貼下面的內容。
- 建好後：
  - repo variable `ANGAREION_REGISTRY_ISSUE` 設成這張 issue 的編號（workflow 的觸發條件與重建腳本都用它）。
  - 各使用者的 `~/.config/angareion/config.json` 的 `registry_issue` 填同一個編號。

## 置頂留言（貼進 issue）

```text
Angareion channel 註冊簿。這裡是 channel 對應的請求入口：angareion channel create／channel delete 會各留一則留言，
workflow 讀整串留言重建 config/angareion/channels.json 後直接更新 main。

留言格式（一行 JSON；不是這個形狀的留言不採計）：

{"schema": 1, "action": "register", "channel": "general", "backend": "github-main", "issue": 123}
{"schema": 1, "action": "unregister", "channel": "general", "backend": "github-main", "issue": 123}

規則：
- 只有 author_association 是 OWNER／MEMBER／COLLABORATOR 的留言會被採計；其他留言自動忽略（也不會被回報）。
- 每次都從整串留言重建，依時間序套用：先註冊名字的人贏（先到者為準），後到的同名註冊不覆蓋並會被回報。
- unregister 只移除指向同一個實體的對應；已經沒有或指到別的實體是 no-op，會被回報。
- channels.json 是重建的產物，不要人工修改。
- 這個 repo 是 public：不要把任何秘密寫進來（channel 名、backend 名、編號與這串留言都是公開的）。
```
