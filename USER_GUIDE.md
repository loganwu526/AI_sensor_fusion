# 四模態 AI 感測融合系統：使用教學與高層 Demo 操作手冊（User Guide）

本手冊說明如何啟動本專案中的所有交付系統（**高層 3 分鐘互動實測 Demo**、**進階工程戰情室**、**公開真實資料集匯入**、**離線架構研究儀表板** 以及 **C++20 嵌入式邊緣核心**），並附上常見問題排解。

---

## 1. 環境檢查與快速啟動

### 1.1 Python 系統（FastAPI 戰情室 ＋ `/demo` 實況簡報）

#### 前置套件確認
* Python **3.10+**（本機已驗證環境為 Python 3.13.5）
* 若尚未安裝相依套件，請執行：
  ```powershell
  pip install -r d:\AI_sensor_fusion\system_design\python_poc\requirements.txt
  pip install ultralytics opencv-python python-multipart
  ```
* 本地真實姿態模型權重 [yolov8n-pose.pt](./system_design/python_poc/yolov8n-pose.pt) 與公開實驗室資料集 [espressif_esp_csi_real.csv](./system_design/python_poc/datasets/real_public/espressif_esp_csi_real.csv) 已內置於專案中，無須連線外網即可離線啟動。

#### 一鍵啟動伺服器（自動避開被佔用的連接埠）
```powershell
python d:\AI_sensor_fusion\system_design\python_poc\run_server.py
```
`run_server.py` 會依序探測 `[8000, 8001, 8080, 8765, 8888, 18000]` 並綁定第一個可用埠（本機目前通常會自動選擇 **`8080`** 埠）。啟動成功後終端機會印出實際網址：
* **高層 3 分鐘實測頁面**：`http://127.0.0.1:8080/demo`（請依實際終端機印出之埠號為準）
* **進階工程戰情室**：`http://127.0.0.1:8080/`
* **互動式 Swagger API 文件**：`http://127.0.0.1:8080/docs`

### 1.2 免伺服器離線體驗項目
* **研究報告互動儀表板**：直接用瀏覽器雙擊開啟 [interactive_dashboard.html](./interactive_dashboard.html)（單檔零外部 CDN，可離線互動體驗四模態雷達圖切換、15 項應用與三階雙軌 BOM 試算）。
* **C++20 嵌入式邊緣核心（Windows 直接雙擊即可看結果）**：
  * 雙擊 [edge_core_demo.exe](./system_design/cpp_edge_core/build/edge_core_demo.exe) 檢視五大場景真陽性／防誤報演練、安全清零耗時與 10,000 次熱路徑微基準測試。
  * 雙擊 [test_edge_core.exe](./system_design/cpp_edge_core/build/test_edge_core.exe) 檢視 7 項單元測試（含 0 次動態 Heap 配置審計）。
  * （已建置防閃退與 `-static` 靜態連結，雙擊結束後會提示「按 Enter 鍵關閉視窗...」。）

---

## 2. 教學一：高層 3 分鐘現場實測 Demo 操作（`/demo`）

開啟 `http://127.0.0.1:<port>/demo` 後，介面精簡為三大步驟，可直接按照下列流程向主管或客戶簡報：

### Step 1｜現場真實 `YOLOv8n-pose` 骨架與「零落盤隱私」實測
1. 點擊頁面上方的綠色主按鈕 **`▶ 開啟筆電鏡頭（即時實測）`**，並在瀏覽器跳出授權詢問時點擊「允許」。
   > **若該台電腦沒有攝影機**：可改點旁邊的 **`上傳圖片測試`** 挑選任何含人物的圖片檔，一樣能觸發真實 AI 骨架推理。
2. 請受測者站在鏡頭前揮手、起立或大幅側身傾斜：
   * 右側黑色畫布將即時繪出由本地 **`yolov8n_pose_local`** 模型提取的 **17 個去識別化關節點骨架**。
   * 觀察下方 4 張實測數據卡：即時姿態（`standing` / `sitting` / `fallen`）、**Volatile RAM 覆寫清零實測耗時（`<100 ms`）**、**全零校驗 `TRUE`** 以及 **磁碟寫入圖片數 `0 張`**。
3. 點擊 **`隱藏左側原圖（模擬正式產品視角）`**，示範最終產品部署後管理端與工程師都看不到左邊的原畫面，從根源解決隱私爭議。

### Step 2｜現場踢館：證明單一 IP Camera 必然失效
1. 請主管**直接用手掌或一張紙張完全遮住筆電鏡頭**（模擬濃煙、夜間全暗或浴室遮蔽）。
   > **無鏡頭備援**：點擊右上角紅框按鈕 **`送入全黑遮蔽幀實測`**。
2. 系統於數百毫秒內偵測到 `Brightness < 28` 或 `Sharpness < 18`，立刻於 Step 2 區域跳出醒目紅框告警：**「⚠ 純視覺 AI 已失明（Pure Vision Blind）！」**。
3. 順勢帶出核心論點：**這就是為什麼單靠 IP Camera 無法解決濃煙搜救與隱私空間防護，必須融合 60GHz 雷達＋VOx 熱像＋Wi-Fi CSI。**

### Step 3｜驗明官方 GitHub 真實 ESP32 資料 × 申請 `< $500` 硬體試作預算
1. 檢視左側即時動畫波形圖：此曲線直接讀取自專案內下載的 [espressif_esp_csi_real.csv](./system_design/python_poc/datasets/real_public/espressif_esp_csi_real.csv)（共 **917 筆**真實 ESP32 封包、歷時 **94.96 秒**、真實 RSSI 波動與多路徑 CSI 擾動），並可在下方預覽原始 `CSI_DATA` 封包原文與官方出處。
2. 指引右側的 **「Gate 1 預算申請：\$500 USD 會議室真實 4 感測器 HIL Demo 採購清單」**，說明兩週內只需採購總價 `$120–$450` 的開發板即可接上這同一套融合引擎進行實場展示。

---

## 3. 教學二：進階工程戰情室操作（`/`）

開啟 `http://127.0.0.1:<port>/` 可全面操作與壓力測試所有工程子系統：

1. **資料來源模式切換**：
   * **`合成模擬 (Simulator)`**：搭配下方 11 個按鈕即時切換正常、真異常（衛浴跌倒、BESS 熱失控濃煙、周界入侵、HRC 盲區逼近）與誤報干擾（寵物水蒸氣、EV 煞車餘熱、野狗眩光、電纜水霧、玻璃外行人）。
   * **`公開資料集回放 (Log-Replay)`**：從下拉選單載入 5 條基準時間軸（或 GitHub 真實 ESP32 資料集），使用「播放／暫停／單步 +1 幀／0.5x–2x 倍速／拖曳滑桿」觀察各階段 `Ground Truth` 與 **`單模態必誤報 ✓ 已被四模態過濾`** 徽章。
   * **`混合實況 (Hybrid Live)`**：將時間軸感測訊號結合瀏覽器實況攝影機的硬體隱私閘門：
     * 在平穩 `L0` 狀態下點擊 **`測試硬闖 L0/L1 門禁`**，觀察系統拒絕擷取並在最下方稽核表寫入 `PRIVACY_GATE_REJECTED`。
     * 點擊 **`開啟瀏覽器實況鏡頭`** 後將時間軸拖至異常段升階 `L2`，右側 RGB 畫布即刻顯示真實骨架與清零時間。
2. **一鍵產出高層批次評估報表**：點擊右上綠色 **`★ 一鍵產出高層批次評估報表`**，系統會對 5 大場景共 600 幀時間軸跑完獨立無汙染回放，即時呈現 Precision / Recall / F1、單模態 vs 四模態誤報對照及頻寬節省表。
3. **驗證防抖、降載與 L3 雙因子授權**：
   * 觸發 `L3` 後切回正常事件，觀察進度條倒數 `35 秒`，點擊 **`快進模擬時間 +40s`** 可立即驗證安全降階。
   * 點擊頂部 **`模擬 NPU 滿載降級`**，於 `L2`/`L3` 期間觀察頂部出現黃色橫幅，系統自動將 RGB 降至 `10 fps` 保障雷達與熱像 `20 Hz` 追蹤。
   * 於 `L3` 授權框輸入操作員 ID（如 `sec-op-01`）與動態 OTP **`842916`**，點擊 **`授權啟封 L3`** 即可解封受控預覽，點 **`解除警報復歸`** 即退回 `L1`。

---

## 4. 教學三：匯入您自己的公開或實測 CSV 資料集

系統支援直接透過 **網頁介面** 或 **命令列工具** 匯入真實外部公開格式：

### 方法 A：透過戰情室網頁匯入
在戰情室卡片「2. 時間軸回放控制」最下方，選擇本機檔案 → 選取格式（`自動偵測` / `ESP-CSI CSV` / `TI mmWave CSV` / `Thermal CSV` / `Unified JSONL`）→ 選擇對應評估場景（`A1`–`B2`）→ 點擊 **`匯入並回放`** 即可。

### 方法 B：透過 CLI 工具轉換為 Unified JSONL
```powershell
python d:\AI_sensor_fusion\system_design\python_poc\scripts\import_public_dataset.py `
  --input path\to\your_csi.csv `
  --format esp_csi `
  --scenario A1
```
轉換後會存於 `system_design/python_poc/datasets/uploaded/`，重新整理或重啟伺服器即自動列於選單中。

### 支援的外部 CSV 欄位格式範例
* **Espressif ESP-CSI CSV**（帶標頭或無標頭 `CSI_DATA,...` 皆可）：
  ```csv
  type,id,mac,rssi,local_timestamp,real_timestamp,data
  CSI_DATA,1,82:FB:A9:3F:02:09,-45,1988046,1.704,"[10 -24 15 -18 22 -6 18 9]"
  ```
* **TI mmWave 點雲 CSV**（依 `frame` 或 `timestamp_ms` 自動聚合 3D 點雲）：
  ```csv
  frame,timestamp_ms,x,y,z,vx,vy,vz,snr
  1,2000,0.1,2.2,1.25,0.0,0.2,0.0,18.5
  ```
* **Melexis MLX90640 / 熱像矩陣 CSV**：每列為 `768` 欄（`32×24`，自動 2×2 池化為 `16×12`）或 `192` 欄溫度浮點數值（可於首欄選擇性附上 `timestamp_ms`）。

---

## 5. 開發、測試與常見問題排除（FAQ）

### 5.1 執行完整自動化測試

* **Python 完整測試套件（21 項單元與端點測試）**：
  ```powershell
  python -m pytest d:\AI_sensor_fusion\system_design\python_poc\tests\test_system.py -v
  ```
* **C++20 重新編譯與單元測試（CMake + MinGW64）**：
  ```powershell
  & "C:\Program Files\CMake\bin\cmake.exe" -S d:\AI_sensor_fusion\system_design\cpp_edge_core -B d:\AI_sensor_fusion\system_design\cpp_edge_core\build -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release
  & "C:\Program Files\CMake\bin\cmake.exe" --build d:\AI_sensor_fusion\system_design\cpp_edge_core\build --parallel
  & "C:\Program Files\CMake\bin\ctest.exe" --test-dir d:\AI_sensor_fusion\system_design\cpp_edge_core\build --output-on-failure
  ```

### 5.2 常見問題與解決對策（FAQ）

| 問題現象 | 原因與排查方式 | 解決方式 |
| :--- | :--- | :--- |
| **`WinError 10048` 無法綁定 8000 埠** | 本機已有其他服務佔用 `8000` 或 `8001` 埠。 | 改用 `python run_server.py` 啟動，腳本會自動探測並改綁 `8080` 或其他空閒埠，並於終端機印出實際網址。 |
| **瀏覽器點擊開啟鏡頭沒反應或被拒絕** | 使用了非 `localhost` / `127.0.0.1` 的區域網 HTTP IP（瀏覽器安全限制僅允許 HTTPS 或 `127.0.0.1` 呼叫 `getUserMedia`），或尚未授權攝影機。 | 務必使用 `http://127.0.0.1:<port>/demo` 或 `http://localhost:<port>/demo` 開啟，並允許網址列左側攝影機權限；無實體鏡頭時可用「上傳圖片測試」或「送入全黑遮蔽幀實測」。 |
| **環境中斷網會不會卡在下載 YOLO 模型？** | 若未預先準備權重，`ultralytics` 可能嘗試連線下載。 | 本專案已內置本地權重 [yolov8n-pose.pt](./system_design/python_poc/yolov8n-pose.pt)（6.5 MB）並設定 `.ultralytics` 至專案資料夾內；即使刪除該檔案亦會自動切換為純離線 OpenCV HOG/Haar 後端，不會阻斷服務。 |
| **雙擊 C++ `.exe` 閃退或顯示缺 DLL？** | 命令列程式執行僅需 ~8 ms 結束，或非靜態連結缺少 MinGW DLL。 | 本專案已設定 `-static` 編譯並內建 `GetConsoleProcessList` 雙擊偵測；若您自行修改並重新編譯過，請確認以 `CMakeLists.txt` 的 Release 組態重新建置。 |
| **想重置 5 條基準 JSONL 測試軌跡** | 不小心覆蓋或修改了 `datasets/*.jsonl`。 | 執行 `python d:\AI_sensor_fusion\system_design\python_poc\scripts\generate_replay_datasets.py` 即可重新生成標準 600 幀基準資料集。 |
