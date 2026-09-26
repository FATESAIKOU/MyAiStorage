# search

Agora 的讀取介面，函式庫加 CLI：條件篩選、全文搜尋（SQLite FTS5 trigram）、讀取單一 Session、指定新鮮度（附快照時間，未達時附警告，讀取不觸發寫入），以及從原始紀錄重建全部衍生物的指令（tasks 4.2〜4.5；design D5、D9；ADR 0007）。
