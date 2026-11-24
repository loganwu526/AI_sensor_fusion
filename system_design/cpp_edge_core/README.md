# 四模態 AI 感測融合嵌入式 C++20 邊緣核心（`cpp_edge_core`）

本專案為四模態 AI 感測融合平台（無線網路設備 `Wi-Fi 7 / IEEE 802.11bf CSI / BLE AoA / UWB`、`60GHz/77GHz mmWave Radar`、非製冷 `VOx LWIR Thermal` 與 `IP Camera`）之 **C++20 嵌入式邊緣高效能融合核心**，實作零動態記憶體配置（Zero Heap Allocation）關鍵熱路徑與 Privacy-by-Design 硬性安全機制。

---

## 目錄與檔案結構

| 檔案路徑 | 核心職責 |
| :--- | :--- |
| [CMakeLists.txt](./CMakeLists.txt) | C++20 CMake 建置腳本（支援 MinGW64 / Ninja / CTest，開啟 `-Wall -Wextra -Wpedantic -Wconversion -Wshadow`） |
| [include/sensor_types.hpp](./include/sensor_types.hpp) | 強型別 `trivially_copyable` 感測幀（`CsiFrame`, `MmWaveFrame`, `ThermalFrame`, `RgbSkeletonFrame` 與隔離之 `RawRgbFrame`）、奈秒級 IEEE 1588 PTP 時間戳與三階硬體配置表 |
| [include/secure_ring_buffer.hpp](./include/secure_ring_buffer.hpp) | 固定容量 `SecureRingBuffer<T, N>`、模擬 `DMA-BUF` 零拷貝讀寫、及防 Dead-Store Elimination 之 `volatile` + `atomic_signal_fence` 安全清零 `secure_wipe()` |
| [include/sliding_window.hpp](./include/sliding_window.hpp) | 100 ms（`100,000,000 ns`）滑動視窗非同步排序對齊器與雷達 3D/徑向速度一階位置外推補償 |
| [include/ds_fusion.hpp](./include/ds_fusion.hpp) | 無 Heap 配置之 Dempster-Shafer（D-S）證據合併引擎、衝突係數 $K$ 計算與 $K \ge 0.99$ Yager 安全回退防除零機制 |
| [include/privacy_fsm.hpp](./include/privacy_fsm.hpp) | Privacy-by-Design `L0–L3` 四階狀態機、Camera-Off 預設關閉守衛、L2 自動 `secure_wipe()` 全零驗證、30 秒遲滯防抖與 NPU 滿載 Graceful Degradation 降幀保護 |
| [include/scenario_profiles.hpp](./include/scenario_profiles.hpp) | 五大旗艦場景（`A1`, `C4`, `B1`, `C1`, `B2`）多型策略介面、物理防誤報濾波規則與 `EdgeFusionCore` 整合管線 |
| [src/fusion_core.cpp](./src/fusion_core.cpp) | 核心對齊器、D-S 融合、L0–L3 狀態機與五大場景策略之零動態配置實作 |
| [src/main.cpp](./src/main.cpp) | 五大場景真陽性／抗誤報演練、Privacy 安全清零示範與 10,000 次熱路徑微基準測試 |
| [tests/test_edge_core.cpp](./tests/test_edge_core.cpp) | 完整 C++20 單元測試（含全域 `operator new` 零 Heap 配置稽核、溢位與 `<100 ms` 全零驗證、D-S 數值驗證與五大場景覆蓋） |

---

## 核心工程設計亮點

1. **關鍵熱路徑 Zero Heap Allocation**：
   - 所有感測幀結構體皆以 `static_assert(std::is_trivially_copyable_v<T>)` 與 `std::is_standard_layout_v<T>` 把關，佇列與陣列全數採編譯期固定容量 `std::array`。
   - 單元測試透過全域 `operator new` 掛鉤實證連續 100 次完整融合迴圈 **0 次動態記憶體配置**。
2. **Privacy-by-Design 三大工程鐵律實作**：
   - **Camera-Off by Default**：處於 `L0_AmbientRf` 或 `L1_NonVisualVerify` 時，`ingest_raw_rgb_frame()` 硬性拒絕寫入 `RawRgbFrame`。
   - **Volatile RAM + `< 100 ms` 安全清零**：L2 透過 `DMA-BUF` 描述子提取 17 點去識別化骨架 `RgbSkeletonFrame` 後，立即呼叫 `secure_wipe()`（使用 `volatile std::uint8_t*` 逐位元組清零與 `std::atomic_signal_fence(std::memory_order_seq_cst)` 阻斷 DSE 最佳化），實測微秒級完成且逐位元組回讀驗證全零。
   - **遲滯防抖與 Graceful Degradation**：狀態降階受 `30 s` 防抖冷卻保護避免邊界震盪；當 NPU 或記憶體頻寬負載達 `≥ 85%` 時自動將 L2 RGB 幀率由 `30 fps` 降至 `10 fps`，同時保障 `20 Hz` 毫米波雷達與熱像追蹤不中斷。
3. **非同步時空對齊與 D-S 高衝突防護**：
   - `SlidingWindowAligner` 同時依 `ptp_timestamp_ns` 與 `monotonic_rx_ns` 升序排列非同步輸入，於 `100,000,000 ns` 視窗內對齊最近幀並剔除超時資料；對滯後之毫米波雷達幀自動以 3D 速度或徑向速度執行一階線性位置外推。
   - `DempsterShaferEngine` 計算 `{Normal, Alert, Uncertainty}` 合併結果與衝突係數 $K$；當 $K \ge 0.99$ 時安全回退至 Yager 保守規則（避免 $1/(1-K)$ 除零）並觸發 `high_conflict_escalation` 升階仲裁。

---

## Windows 本機編譯與測試指南（CMake + MinGW64）

```powershell
# 1. 使用 CMake + MinGW Makefiles 產生建置系統（Release -O2 模式檢驗防 DSE 清零）
& "C:\Program Files\CMake\bin\cmake.exe" -S d:\AI_sensor_fusion\system_design\cpp_edge_core -B d:\AI_sensor_fusion\system_design\cpp_edge_core\build -G "MinGW Makefiles" -DCMAKE_BUILD_TYPE=Release

# 2. 編譯核心函式庫、Demo 與單元測試
& "C:\Program Files\CMake\bin\cmake.exe" --build d:\AI_sensor_fusion\system_design\cpp_edge_core\build --parallel

# 3. 執行完整單元測試（含 Zero-Heap 稽核）
& d:\AI_sensor_fusion\system_design\cpp_edge_core\build\test_edge_core.exe

# 4. 執行五大場景演練與 10,000 次熱路徑微基準測試
& d:\AI_sensor_fusion\system_design\cpp_edge_core\build\edge_core_demo.exe

# 5. 或透過 CTest 驗證
& "C:\Program Files\CMake\bin\ctest.exe" --test-dir d:\AI_sensor_fusion\system_design\cpp_edge_core\build --output-on-failure
```
