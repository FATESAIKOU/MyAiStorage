# opencode

opencode 同步器（tasks 5.2〜5.3）：定期匯出有變化的 Session，以及「同步並提交」。期 1 在 Mac 上的 Ubuntu 容器裡執行（tasks 5.1）；之後同一套裝進 worker image，由 MyLinuxPool 在刪除 worker 前跑最後一次（`docs/tickets/mylinuxpool.md`）。
