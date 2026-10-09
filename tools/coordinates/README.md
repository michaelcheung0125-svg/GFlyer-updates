# 座標圖鑑的自動更新

`coordinates/coordinates.json` 是兩個 GFlyer App 讀的座標圖鑑（GitHub Pages 網址
`https://michaelcheung0125-svg.github.io/GFlyer-updates/coordinates/coordinates.json`）。
它由這個目錄的 `sync_coordinates.py` 產生，**每 6 小時由 GitHub Actions 自動執行**，不用手動更新，也不用發新版 App。

| 檔案 | 內容 |
|---|---|
| `sync_coordinates.py` | 抓 pikmin.talllkai.com 的明信片與純點，併入活動座標，產生 `coordinates/coordinates.json` |
| `activities.json` | 活動座標，**作者手動維護** |
| `test_sync_coordinates.py` | 產生腳本的測試，每次同步前先跑 |
| `../../.github/workflows/sync-coordinates.yml` | 排程：每 6 小時（香港時間 02:17、08:17、14:17、20:17），另外改了這個目錄的檔案推上來時也會跑 |

## 每次執行發生的事

1. 抓明信片（`/Postcard`，每頁 20 張，請求之間間隔 1 秒）與純點（`/PureSpot/Map`，一次請求）。
2. 照 App 的規則轉成座標庫格式，和現在的 `coordinates/coordinates.json` 比較。
   **內容沒變就什麼都不做**；有變才 revision +1、commit（作者是 `github-actions[bot]`）並推送。
3. 等 GitHub Pages 回傳新的 revision（最多約 15 分鐘，太久會要求 Pages 重新建置一次）。
4. App 下次抓線上版時拿到新資料：Android 每次開 App；iOS 每次啟動後第一次打開圖鑑。

## 什麼時候不會發布

下面幾種情況這次執行會失敗，**線上維持原本的資料**，GitHub 會寄失敗通知信給 repo 擁有者：

- 官網連不上、改版（找不到「共 N 張」、找不到純點的內嵌資料）。
- 純點或明信片比線上版少超過 10%，或原本有、現在變成 0 筆 —— 多半是官網出問題，不是真的下架。
  確定是官網真的大量下架的話：Actions →「同步座標圖鑑」→ Run workflow，勾選 `allow_shrink`。
- `activities.json` 寫錯（缺 id、子分類不存在、名稱前後有空白等）。
- 檔案超過兩個 App 的下載上限 4 MB（算解壓縮後的大小）。縮排版超過 3.5 MB 時會先自動改寫成單行（約小兩成）；
  單行也超過 4 MB 就要先發新版 App 提高上限。

官網上單一筆資料有問題（缺座標、名稱空白、縮圖不是 https）只略過那一筆，過長的名稱或說明照 App 的規則截斷，
不會擋住整份更新；執行紀錄的 summary 會列出來。

## 改活動座標

直接改 `activities.json` 並推送（或在 GitHub 網頁上編輯），workflow 會馬上重新產生並發布。

- `id` 是座標的固定識別：使用者的最愛、造訪標記、前往紀錄都綁在 id 上。**一旦發布就不可改、不可重複使用**。
  `evt-001`〜`evt-023` 都用過了（刪掉的也算，見檔案裡的 `_comment`），新增從 `evt-024` 接續。
- 刪除已結束的活動：直接刪掉那幾筆，其餘座標的 id 不會跟著變。
- `remindDays` 是提醒天數，`startDate` / `endDate` 是活動期間（子分類上，可有可無）。

## 手動執行

- 網頁：repo 的 Actions →「同步座標圖鑑」→ Run workflow。
- 指令：`gh workflow run sync-coordinates.yml --repo michaelcheung0125-svg/GFlyer-updates`
- 本機（只看結果、不寫檔）：`python tools/coordinates/sync_coordinates.py --dry-run`

本機真的產生並推送也可以，但通常不需要。**不要在別的地方產生座標庫再複製過來**：App 只接受比手上更大的
revision，`coordinates/coordinates.json` 是 revision 唯一的基準。

## 要注意的事

- **這個 repo 每天可能有機器人的 commit**，本機的 clone 推送前先 `git pull --rebase origin main`
  （兩個 App 的發版流程本來就有這一步）。機器人只改 `coordinates/coordinates.json`，不會有衝突。
- GitHub 的排程可能延遲幾分鐘到幾小時，忙的時候甚至會略過（2026-10-08 改成一天一次時，第一次晚了約 4 小時，第二天那次過了兩個半小時都還沒跑），所以一天排四次。官網沒有變就不發布，App 不會因為多跑幾次而多下載。public repo **連續 60 天沒有任何活動**（包括機器人的 commit）時，
  GitHub 會自動停用排程並寄信通知；到 Actions 頁面按「Enable workflow」即可。
- Android APK 內建的種子是線上版的快照：發版前在 GFlyer repo 跑 `python tools/update_coordinate_seed.py`
  把種子更新到線上的最新 revision（不跑也可以，App 開啟後會自己抓線上版）。
- 資料來源是凱哥（TalllKai）社群網站的公開內容，App 內圖鑑頁尾標示「皮克敏純點明信片地圖 pikmin.talllkai.com」。
  User-Agent 是 `GFlyer-coordinate-sync/1.0`，每次約 28 次請求、間隔 1 秒，一天四次約 112 次。
