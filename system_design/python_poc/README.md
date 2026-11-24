# 四模態 AI 感測融合與 Privacy-by-Design 系統原型（Python FastAPI POC）

本專案為「無線網路設備（Wi-Fi 802.11bf CSI / BLE / UWB）＋ 60/77GHz mmWave Radar ＋ 非製冷 VOx Thermal ＋ Edge IP Camera」四模態異質感知系統之**可本機啟動與驗證之工程原型（POC）與即時監控戰情室**。

## 專案目錄結構

```text
python_poc/
├── requirements.txt             # Python 套件相依清單
├── README.md                    # 本專案說明與啟動指南
├── app/
│   ├── __init__.py
│   ├── main.py                  # FastAPI REST API 與 8 Hz WebSocket 遙測伺服器
│   ├── models.py                # Pydantic v2 四模態感測幀、D-S、硬體與狀態機模型
│   ├── fusion_engine.py         # 100ms 滑動視窗對齊（含卡爾曼外推）+ D-S 證據合併引擎
│   ├── state_machine.py         # Privacy-by-Design L0–L3 狀態機、防抖、降級保護與 RAM 清零
│   ├── simulator.py             # 異步四模態模擬器與 11 種標準/異常/誤報干擾情境注入器
│   └── modules/
│       ├── base.py              # ScenarioModule 抽象插件介面
│       ├── a1_elderly_care.py   # A1 銀髮長照跌倒與呼吸守護（過濾寵物與水蒸氣）
│       ├── c4_bess_thermal.py   # C4 BESS 熱失控 Stage 1 截斷與濃煙穿透搜救
│       ├── b1_perimeter.py      # B1 周界入侵與 BLE/UWB 白名單＋反光動物過濾
│       ├── c1_hrc_safety.py     # C1 HRC 人機防撞與 UWB 跨貨架＋電纜水霧誤急停消除
│       └── b2_smart_hvac.py     # B2 商辦 HVAC 零 False-Off 與玻璃外行人排除
├── static/
│   └── index.html               # 單檔零外部 CDN 依賴即時監控戰情室（Canvas + 深淺色切換）
└── tests/
    └── test_system.py           # Pytest 全自動化單元與端點整合測試套件
```

## 快速啟動方式

```bash
cd d:\AI_sensor_fusion\system_design\python_poc
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

啟動後於瀏覽器開啟 `http://127.0.0.1:8000/` 即可檢視即時監控戰情室（支援深淺色切換、熱切換五大場景、切換三大硬體形態、注入 11 種測試情境、快進測試遲滯防抖及雙因子授權啟封 L3）。

## 執行自動化測試

```bash
pytest d:\AI_sensor_fusion\system_design\python_poc\tests\test_system.py -v
```

## REST 與 WebSocket API 摘要

- `GET /`：單檔零依賴即時監控戰情室
- `GET /api/health`：系統健康狀態檢查
- `GET /api/state`：取得當前完整 `SystemStateSnapshot`
- `GET /api/scenarios`：列出五大場景、三大硬體藍本與 11 種可注入事件目錄
- `POST /api/scenario/{module_id}`：熱切換場景模組（`A1` / `C4` / `B1` / `C1` / `B2`）
- `POST /api/hardware/{profile_id}`：切換硬體形態（`phase1_distributed_mvp` / `phase2_all_in_one_tier2` / `tier3_industrial`）
- `POST /api/inject/{event_name}`：注入測試情境（含正常、真異常、誤報干擾）
- `POST /api/fast-forward/{seconds}`：快進模擬時間以驗證 L3/L2/L1 防抖降階
- `POST /api/stress/{enabled}`：啟閉 NPU 高算力滿載模擬，驗證 Graceful Degradation
- `POST /api/l3/authorize`：雙因子授權（OTP）啟封 L3 加密存證或解除警報
- `GET /api/audit`：取得 Volatile RAM `<100ms` 覆寫清零與北向聯動稽核日誌
- `WS /ws/telemetry`：約 8 Hz 即時遙測推送通道
