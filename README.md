# 四模態 AI 感測融合架構研究專案（無線網路設備 × IP Camera × Thermal × mmWave）

本專案針對**「無線網路設備（Wi-Fi 7 / IEEE 802.11bf / BLE AoA / UWB）」、「IP Camera（RGB / Starlight / IR-Cut）」、「Thermal Sensor（非製冷 $\text{VO}_x$ LWIR 熱像）」與「mmWave Radar（60GHz / 77GHz FMCW / 4D 成像雷達）」**之異質多模態感測融合進行系統性工程與商業可行性研究，提出突破傳統「感測孤島」、誤報疲勞與隱私合規壁壘的下一代邊緣感知藍圖。

---

## 🎯 研究核心目標

1. **物理盲區閉環驗證**：量化四類設備於九大工程維度之物理極限，論證為何任兩兩組合皆具致命缺口，唯有四模態正交協同方能兼顧穿牆、穿濃煙／水氣、輻射測溫、亞毫米呼吸微動與高語意取證。
2. **重構無線基礎設施定位（ISAC）**：將企業與商用 Wi-Fi 7 AP／邊緣閘道從被動網路管線升級為「確定性骨幹＋Edge AI 融合節點＋無線物理感測器」三重角色中樞。
3. **建立 Privacy-by-Design 技術與法規護城河**：設計 **L0–L3 四階分級喚醒狀態機**，落實「預設不開鏡、記憶體揮發即時銷毀、授權加密存證」，直接對齊歐盟 GDPR Art. 25、EU AI Act、HIPAA、RED EN 18031 與 EU CRA 監管門檻。
4. **落地可行之雙軌 BOM 與商業路徑**：建立區分 **非 NDAA 性價比路線** 與 **美國 NDAA Section 889 合規路線** 之消費／商用／工業三階 BOM 模型，並規劃由 Brownfield 利舊閘道切入至 AP-as-a-Sensor 平台授權之 42 個月 GTM 藍圖。

---

## 📂 專案交付物導覽與檔案清單

| 交付文件 | 類型 | 內容定位與適用讀者 |
| :--- | :--- | :--- |
| **[SYSTEM_ARCHITECTURE.md](./SYSTEM_ARCHITECTURE.md)** | 系統架構文件 | 對應實作系統的雙軌模組地圖、端對端資料流與 Mermaid 圖、100ms 對齊／D-S／L0–L3 規格、YOLOv8n-pose 實況引擎、硬體基準及完整 24 個 API 端點表。 |
| **[USER_GUIDE.md](./USER_GUIDE.md)** | 使用教學與操作手冊 | 快速啟動指令、`/demo` 高層 3 分鐘現場實測腳本、`/` 戰情室操作、公開 CSV 匯入格式、C++20 編譯測試及常見問題 FAQ。 |
| **[research_report.md](./research_report.md)** | 完整深度白皮書 | 涵蓋高階執行摘要、四模態物理互補矩陣、15 大跨領域應用與 Top 5 旗艦深潛、L0–L3 系統架構、亞微秒時空標定、All-in-One vs. 分散式權衡、Edge AI 延遲預算、五大競品剖析、三階雙軌 BOM、毛利／ROI、法規護城河與 GTM 路線圖。 |
| **[prd_proposal_outline.md](./prd_proposal_outline.md)** | 實戰 PRD 與簡報大綱 | 包含 1 頁式高階主管摘要、14 頁對內／對外商業提案大綱（含建議圖表）、MVP（Phase 1 Brownfield 閘道）與 V2（Tier 2/3 量產硬體）產品需求規格、KPI 驗收門檻、24 個月開發里程碑及技術／法規／市場風險緩解矩陣。 |
| **[interactive_dashboard.html](./interactive_dashboard.html)** | 離線互動視覺化儀表板 | 單檔自包含（零外部 CDN 依賴，支援深色／淺色切換），內建四大互動工具：四模態融合雷達模擬器、15 項應用篩選與 Top 5 展開卡片、L0–L3 隱私喚醒與功耗／頻寬／延遲模擬、All-in-One vs. 分散式與三階雙軌 BOM 試算器。 |
| **[sections/01_applications_and_matrix.md](./sections/01_applications_and_matrix.md)** | 專題底稿（第一章） | 四模態九維對照矩陣、盲區互補閉環、四大領域 15 項應用全景掃描與 Top 5 加權評分細節。 |
| **[sections/02_technical_architecture.md](./sections/02_technical_architecture.md)** | 專題底稿（第二章） | 無線設備三重角色與 802.11bf-2025 五步時序、融合層級比較、L0–L3 狀態機、PTP 與異質標定數學推導及延遲預算拆解。 |
| **[sections/03_market_bom_gtm.md](./sections/03_market_bom_gtm.md)** | 專題底稿（第三章） | 五大競品陣營缺口（含 Origin AI 併購動態）、Tier 1–3 分項報價與萬片 BOM、四大商業模式、FCC/ETSI/NCC/EU CRA 法規詳解與 GTM。 |

---

## 🛠️ 系統工程設計與雙軌可執行原型（`system_design/`）

基於上述研究結論，本專案進一步完成**全模組化可插拔架構**（優先涵蓋 `A1`、`C4`、`B1`、`C1`、`B2` 五大旗艦場景）與**三大硬體部署藍本**（Phase 1 分散式利舊、Phase 2 All-in-One Tier 2 智慧 AP、Tier 3 工業寬溫 IP67）之工程設計與雙軌程式碼實作：

| 工程交付模組 | 路徑 | 內容說明與關鍵技術 |
| :--- | :--- | :--- |
| **系統與融合核心規格書** | **[system_design/docs/01_system_architecture_spec.md](./system_design/docs/01_system_architecture_spec.md)** | 邊緣微服務進程架構、L0–L3 狀態機（含 30–60s 防抖與降級保護）、D-S 證據合併與 100ms 對齊數學、MQTT/gRPC Protobuf/ONVIF 協定與 PTP 標定 SOP。 |
| **三大形態硬體工程設計包** | **[system_design/docs/02_hardware_engineering_pack.md](./system_design/docs/02_hardware_engineering_pack.md)** | 三大形態子系統方塊圖、MIPI/VoSPI/SPI/UART/1PPS 腳位矩陣、PoE++ 電源樹與 25.5W 降頻規避保護、10–12 層 PCB EBG/Via Fence、60GHz Radome 半波長（±0.05 mm）與 $\text{VO}_x$ 隔熱腔／20°–35° 雲台機構、雙軌工程 BOM。 |
| **五大場景模組規格書** | **[system_design/docs/03_scenario_modules_spec.md](./system_design/docs/03_scenario_modules_spec.md)** | 統一 `ScenarioModule` 抽象介面與 `A1` / `C4` / `B1` / `C1` / `B2` 五大場景閾值、防誤報抑制碼與北向（MQTT/SIP/Modbus TCP/ONVIF/PROFINET/BACnet）聯動規格。 |
| **Python FastAPI 原型與即時戰情室** | **[system_design/python_poc/README.md](./system_design/python_poc/README.md)** | FastAPI + 8 Hz WebSocket 即時遙測、100ms 卡爾曼對齊 + D-S 融合、L0–L3 狀態機 + RAM 清零驗證、五大場景模組、11 種測試情境注入與單檔零外部依賴監控戰情室（`static/index.html`）。 |
| **C++20 嵌入式邊緣融合核心** | **[system_design/cpp_edge_core/README.md](./system_design/cpp_edge_core/README.md)** | 零動態記憶體配置（Zero Heap Allocation）熱路徑、`SecureRingBuffer` + `volatile` 防 DSE `<100 ms` 強制清零、100ms 滑動視窗對齊、D-S 引擎與 CTest 單元測試。 |

### 快速啟動與實測指令

1. **啟動 Python FastAPI 即時戰情室與 API**：
   ```powershell
   cd d:\AI_sensor_fusion\system_design\python_poc
   pip install -r requirements.txt
   uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
   # 瀏覽器開啟 http://127.0.0.1:8000/
   ```
2. **執行 Python 自動化測試（16 項單元與 API/WebSocket 測試）**：
   ```powershell
   python -m pytest d:\AI_sensor_fusion\system_design\python_poc\tests\test_system.py -v
   ```
3. **編譯並執行 C++20 嵌入式邊緣核心（CMake + MinGW64）**：
   ```powershell
   & "C:\Program Files\CMake\bin\cmake.exe" -S d:\AI_sensor_fusion\system_design\cpp_edge_core -B d:\AI_sensor_fusion\system_design\cpp_edge_core\build -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release
   & "C:\Program Files\CMake\bin\cmake.exe" --build d:\AI_sensor_fusion\system_design\cpp_edge_core\build --parallel
   & d:\AI_sensor_fusion\system_design\cpp_edge_core\build\test_edge_core.exe
   & d:\AI_sensor_fusion\system_design\cpp_edge_core\build\edge_core_demo.exe
   ```

---

## 💡 核心設計理念

### 1. 無線設備三重角色定位（Network-as-a-Sensor-Hub / ISAC）
* **確定性傳輸骨幹**：Wi-Fi 7（802.11be MLO STR/EMLSR）＋ IEEE 802.3bt PoE++（60–90 W）＋ 2.5G/10GbE／5G 備援，實施 2.4/5 GHz 感知與 6 GHz 高速通道隔離（ISAC Co-Scheduling）。
* **Edge AI 融合與時鐘中樞**：承載 6–100+ TOPS NPU 執行異質融合與本地 IEEE 1588 PTP（< 1 μs）時間同步，消除高頻寬 Raw Stream 上雲成本。
* **無線物理感測器**：整合 **IEEE 802.11bf-2025（WLAN Sensing）** Sub-7 GHz 跨牆感知與 60 GHz 微動感測，結合 BLE AoA（視距 0.1–0.5 m／室內 0.3–1.0 m）與 UWB（視距 $\le 10\text{ cm}$／多路徑 10–30 cm）白名單識別。

### 2. Privacy-by-Design 四階分級喚醒狀態機（L0 → L3）
遵循**「預設不開鏡（Camera-Off by Default）」、「記憶體揮發隔離（Zero Disk Persistence）」與「雙重授權加密託管」**三大鐵律：
* **L0（常駐無線巡檢）**：僅開 Wi-Fi CSI / BLE；雷達處 LPDS（~0.5 mW）、熱像與攝影機關斷／待機；增量功耗 **~0.3–0.8 W**（含 AP 總功耗 12.5–19 W），對外上行 **< 2–5 kbps**，達最高 Tier-0 零個資等級。
* **L1（非視覺雙重驗證）**：2 ms 喚醒 60GHz 雷達＋低解析熱陣列，以 D-S 證據理論驗證 3D 高度跌落、呼吸微多普勒與體溫／過熱輪廓，對外 **5–20 kbps**（零攝影機介入）。
* **L2（端側去識別化推論）**：模糊高危事件熱喚醒 RGB CMOS 進 **TEE Volatile RAM**，執行 INT8 `YOLOv8n-pose` 與 Cross-Attention 提取 17 點骨架座標與匿名框後 **< 100 ms 內覆寫銷毀原始像素**，對外僅輸出 **15–60 kbps JSON**（滿足 GDPR Art. 25）。
* **L3（授權加密存證）**：連續幀確認危急或經雙因子審計授權後，封裝 Pre-roll 5–10 秒緩衝為 H.265（15–30 Mbps 突發），以 **AES-256-GCM + TLS 1.3** 加密傳輸並遮罩無關第三方。透過熱待機（Warm-Standby）策略消除冷啟動延遲，**預熱路徑端對端告警達 110–205 ms（標準逐級 250–420 ms）**，常態回傳頻寬與儲存節省 **> 90%–95%**。

---

## 📊 核心結論速覽

### Top 5 旗艦應用排行（五維度加權 100 分制）
1. **【93.3 分】銀髮居家與長照衛浴／臥室零穿戴跌倒與呼吸守護（A1）**：解決 23% 長者穿戴未依規率與 80%–99% 警報疲勞，`[合理工程推估]` 實務誤報率再降 **75%–90%**、全屋常態頻寬省 **>95%**、反應時間 **1.5–3.0 秒**。
2. **【90.9 分】鋰電池儲能站（BESS）／EV 超充站熱失控截斷與濃煙搜救（C4）**：Thermal 於 Stage 1 提前煙感數分鐘切斷電源（配電／變電基準降低非計畫停機達 **73%**），災時濃煙 0 m 能見度下 mmWave 維持 **90%+ 定位能力**。
3. **【88.1 分】關鍵園區／倉儲全天候零誤報周界入侵偵測（B1）**：消除 PIR 每日 20–50 次誤報，綜合壓低至 **<1–2 次/百公尺/月（降 >98%）**，較全面部署雙光熱像 PTZ 省 **40%–60%** 硬體成本。
4. **【87.7 分】智慧工廠人機協作（HRC）／倉儲粉塵冷鏈盲區防撞（C1）**：符合 ISO/TS 15066，UWB 跨貨架預警＋雷達熱像排除晃動電纜，削減 **70%–85% 誤急停**，本地 PLC 聯動延遲 **< 50–150 ms**。
5. **【87.3 分】智慧商辦微氣候 HVAC 零 False-Off 節能與夜間安防（B2）**：複用天花板 AP＋32×24 熱像防玻璃外誤觸，將無人關機延遲由 30–60 分鐘縮短至 **5–10 分鐘**，額外釋放 **15%–25% 節能空間**。

### 三階雙軌硬體定位與萬片級 BOM 成本（USD）

| 產品階級 | 目標定位 | 代表晶片架構（非 NDAA / NDAA 雙軌） | All-in-One 估算 BOM | 分散式單空間攤提 BOM |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1**<br>消費／入門商用 | 目標 \$65–\$120<br>居家照護／小型店面 | ESP32-C6 / MT7981B + TI IWRL6432 + MLX90640 + RV1106/1126（非 NDAA）或 Kneron KL630/720（合規）+ IMX307 | 非 NDAA：**\$61.80–\$88.00**<br>NDAA：**\$81.80–\$110.50** | 1 閘道＋3 房無鏡頭節點：<br>**\$53–\$61 / 空間** |
| **Tier 2**<br>中高階商用 | 目標 \$180–\$350<br>長照機構／商辦／零售 | IPQ5424 / Filogic + TI IWR6843AOP / BGT60TR13C + Tiny1-C / FLIR Lepton 3.5 + RK3588（非 NDAA）或 QCS6490/CV72（合規）+ IMX415/678 | 非 NDAA：**\$189.00–\$260.00**<br>NDAA：**\$257.00–\$355.00** | 1 走廊 AP＋4 房無鏡頭 Pod：<br>**\$141–\$174 / 空間** |
| **Tier 3**<br>工業／關鍵基建 | 目標 \$550–\$1,200+<br>BESS／半導體／港口交通 | Filogic 880 / IPQ9574 + PoE++ + TI AWR2944（或 4× AWR2243 級聯）+ Seek Mosaic 320 / InfiRay 384 / FLIR Boson 640 + CV5 / Jetson Orin NX + IMX678 | 準工業：**\$649.00–\$960.00**<br>關鍵基建旗艦：**\$2,314–\$3,695** | 1 工業閘道＋4 純感測前端：<br>**\$600–\$720 / 點位** |

### 商業模式、法規護城河與 GTM 路徑
* **獲利結構**：結合買斷＋無變磚雙層 SaaS（硬體毛利 **30%–56%**、SaaS 毛利 **78%–86%**），**第 3 年綜合混合毛利率達 64%–72%**；百床長照 HaaS（\$55 PBM）約 **7.5–12 個月回本**，20 點位半導體／變電室試點 **3–6 個月回本**。
* **法規避險**：嚴格將 **77 GHz 雷達隔離於交通／港口與封閉桶槽**，室內全面採用 **60 GHz**（FCC 15.255 / NCC LP0002），預備 **RED EN 18031** 與 **EU CRA（2026/09 通報、2027/12 強制）** SBOM 安全機制。
* **三階段 GTM**：**Phase 1（M1–M12）** 以 Brownfield 利舊融合閘道試點長照與 BESS 驗證 ROI $\to$ **Phase 2（M12–M24）** 完成射頻／資安取證與 Tier 2/3 量產 $\to$ **Phase 3（M24–M42）** 輸出 802.11bf AP-as-a-Sensor SDK 授權全球網通 ODM。
