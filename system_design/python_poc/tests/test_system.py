"""四模態 AI 感測融合與 Privacy-by-Design 系統原型完整自動化測試套件。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# 確保可從任意工作目錄匯入 python_poc 專案套件
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.fusion_engine import DempsterShaferFusionEngine, SlidingWindowAligner
from app.main import app, fusion_system
from app.models import (
    CsiFrame,
    HardwareProfileId,
    MmWaveFrame,
    MmWavePoint,
    ModalityBpa,
    PrivacyLevel,
    RgbFrameMetadata,
    RiskLevel,
    ScenarioId,
    ThermalFrame,
)
from app.state_machine import PrivacyStateMachine, VolatileRamBuffer


# ---------------------------------------------------------------------------
# 1. Dempster-Shafer (D-S) 證據合併數學精度與高衝突安全回退測試
# ---------------------------------------------------------------------------


def test_ds_combination_exact_math() -> None:
    """驗證雙模態 D-S 正交合併公式之手動計算精確度（Normal, Alert, Θ, K, Bel, Pl）。"""
    # m1 = (N=0.2, A=0.6, U=0.2)
    # m2 = (N=0.1, A=0.7, U=0.2)
    # 衝突 K = 0.2*0.7 + 0.6*0.1 = 0.14 + 0.06 = 0.20
    # 1 - K = 0.80
    # u(N) = 0.2*0.1 + 0.2*0.2 + 0.2*0.1 = 0.08 -> m(N) = 0.08 / 0.80 = 0.10
    # u(A) = 0.6*0.7 + 0.6*0.2 + 0.2*0.7 = 0.42 + 0.12 + 0.14 = 0.68 -> m(A) = 0.68 / 0.80 = 0.85
    # u(Θ) = 0.2*0.2 = 0.04 -> m(Θ) = 0.04 / 0.80 = 0.05
    b1 = ModalityBpa(modality="mmWave", m_normal=0.2, m_alert=0.6, m_uncertainty=0.2)
    b2 = ModalityBpa(modality="Thermal", m_normal=0.1, m_alert=0.7, m_uncertainty=0.2)

    res = DempsterShaferFusionEngine.combine_bpas([b1, b2])

    assert res.conflict_k == pytest.approx(0.20, abs=1e-5)
    assert res.m_normal == pytest.approx(0.10, abs=1e-5)
    assert res.m_alert == pytest.approx(0.85, abs=1e-5)
    assert res.m_uncertainty == pytest.approx(0.05, abs=1e-5)
    assert res.belief_alert == pytest.approx(0.85, abs=1e-5)
    assert res.plausibility_alert == pytest.approx(0.90, abs=1e-5)
    assert res.high_conflict_flag is False
    assert (res.m_normal + res.m_alert + res.m_uncertainty) == pytest.approx(1.0, abs=1e-5)


def test_ds_high_conflict_guard_prevents_div_zero() -> None:
    """驗證當兩模態極端矛盾（K >= 0.99）時觸發防除零回退並標記高衝突須升階仲裁。"""
    b_norm = ModalityBpa(modality="Thermal", m_normal=0.995, m_alert=0.0, m_uncertainty=0.005)
    b_alrt = ModalityBpa(modality="mmWave", m_normal=0.0, m_alert=0.996, m_uncertainty=0.004)

    res = DempsterShaferFusionEngine.combine_bpas([b_norm, b_alrt])

    assert res.conflict_k >= 0.99
    assert res.high_conflict_flag is True
    assert res.m_uncertainty > 0.90
    assert (res.m_normal + res.m_alert + res.m_uncertainty) == pytest.approx(1.0, abs=1e-5)


# ---------------------------------------------------------------------------
# 2. 100ms 滑動視窗對齊器、卡爾曼外推與超時丟棄測試
# ---------------------------------------------------------------------------


def test_sliding_window_100ms_alignment_and_kalman_extrapolation() -> None:
    """驗證 100ms 視窗對齊、超出 100ms 幀剔除，以及 mmWave 卡爾曼速度外推補償。"""
    aligner = SlidingWindowAligner(window_ms=100.0, tolerance_ms=15.0)

    # 推入一筆超時過舊的 Thermal 幀（距離 target=1000ms 超過 100ms）
    aligner.push_thermal(
        ThermalFrame(timestamp_ms=850.0, max_temp_c=99.0, grid_flat=[24.0] * 192)
    )
    # 推入兩筆 100ms 視窗內的前後 Thermal 幀（960ms 與 1040ms）用於線性插值
    aligner.push_thermal(
        ThermalFrame(timestamp_ms=960.0, max_temp_c=34.0, hotspot_x=0.4, grid_flat=[34.0] * 192)
    )
    aligner.push_thermal(
        ThermalFrame(timestamp_ms=1040.0, max_temp_c=38.0, hotspot_x=0.6, grid_flat=[38.0] * 192)
    )

    # 推入兩筆 CSI 測試高頻聚合
    aligner.push_csi(CsiFrame(timestamp_ms=990.0, fresnel_variance=0.40))
    aligner.push_csi(CsiFrame(timestamp_ms=998.0, fresnel_variance=0.60))

    # 推入 mmWave 幀（距離 target=1000ms 落後 50ms，垂直速度 Vz=-1.0 m/s）
    aligner.push_mmwave(
        MmWaveFrame(
            timestamp_ms=900.0,
            centroid_z_m=1.50,
            vz_mps=-1.0,
            points=[MmWavePoint(x=0.0, y=2.0, z=1.50, vz=-1.0)],
        )
    )
    aligner.push_mmwave(
        MmWaveFrame(
            timestamp_ms=950.0,
            centroid_z_m=1.45,
            vz_mps=-1.0,
            points=[MmWavePoint(x=0.0, y=2.0, z=1.45, vz=-1.0)],
        )
    )

    aligned = aligner.align(target_timestamp_ms=1000.0)

    assert aligned.csi is not None
    assert aligned.csi.fresnel_variance == pytest.approx(0.50, abs=1e-4)

    # mmWave 落後 50ms (=0.05s)，Vz=-1.0 m/s -> Z 應從 1.45 外推至約 1.40m
    assert aligned.mmwave is not None
    assert aligned.mmwave_extrapolated is True
    assert aligned.mmwave_offset_ms == pytest.approx(50.0, abs=1e-3)
    assert aligned.mmwave.centroid_z_m == pytest.approx(1.40, abs=0.03)

    # Thermal 插值於 960ms (34°C) 與 1040ms (38°C) 中點 1000ms -> 36.0°C（850ms 過時幀不採計）
    assert aligned.thermal is not None
    assert aligned.thermal.max_temp_c == pytest.approx(36.0, abs=0.05)
    assert aligned.thermal.hotspot_x == pytest.approx(0.5, abs=0.01)

    # 若目標時間推移至 1200ms，所有先前幀皆超出 100ms 視窗應全部被剔除
    stale_aligned = aligner.align(target_timestamp_ms=1200.0)
    assert stale_aligned.csi is None
    assert stale_aligned.mmwave is None
    assert stale_aligned.thermal is None


# ---------------------------------------------------------------------------
# 3. Volatile RAM <100ms 清零、L0-L3 狀態機升降階防抖與 Graceful Degradation 測試
# ---------------------------------------------------------------------------


def test_volatile_ram_wipe_under_100ms() -> None:
    """驗證 Volatile RAM 隔離緩衝區實際覆寫為全 0x00 且耗時遠低於 100 ms 規範。"""
    buf = VolatileRamBuffer(buffer_bytes=262_144)
    buf.stage_raw_yuv_frame(seed_byte=0xC3)
    assert any(buf.read_raw_snapshot())

    wipe_ms, verified = buf.wipe_and_verify()
    assert verified is True
    assert wipe_ms < 100.0
    assert not any(buf.read_raw_snapshot())


def test_state_machine_escalation_hysteresis_and_degradation() -> None:
    """測試 L0->L1->L2->L3 升級、35 秒防抖降級（快進時間）與 NPU 滿載降幀保護。"""
    fsm = PrivacyStateMachine(
        profile_id=HardwareProfileId.PHASE1_DISTRIBUTED_MVP,
        hysteresis_s=35.0,
        critical_confirm_frames=3,
    )
    aligner = SlidingWindowAligner()
    ds_engine = DempsterShaferFusionEngine()
    from app.modules.a1_elderly_care import A1ElderlyCareModule

    mod = A1ElderlyCareModule()

    # Step 1: 空場維持 L0
    aligner.push_csi(CsiFrame(timestamp_ms=100.0, fresnel_variance=0.05))
    al = aligner.align(100.0)
    ds = ds_engine.fuse(al, ScenarioId.A1, fsm.current_level)
    ev = mod.evaluate(al, ds)
    fsm.step(al, ds, ev, override_sim_time_s=0.1)
    assert fsm.current_level == PrivacyLevel.L0

    # Step 2: 長者正常活動 -> 升至 L1（嚴格不開鏡）
    aligner.push_csi(CsiFrame(timestamp_ms=500.0, fresnel_variance=0.42))
    aligner.push_mmwave(
        MmWaveFrame(timestamp_ms=500.0, centroid_z_m=1.25, micro_doppler_human_score=0.80, breathing_bpm=16.0)
    )
    al = aligner.align(500.0)
    ds = ds_engine.fuse(al, ScenarioId.A1, fsm.current_level)
    ev = mod.evaluate(al, ds)
    fsm.step(al, ds, ev, override_sim_time_s=0.5)
    assert fsm.current_level == PrivacyLevel.L1
    sensors, _, _, _ = fsm.compute_metrics()
    assert sensors.rgb_fps == 0.0

    # Step 3: 發生疑似跌倒（CRITICAL）第 1 幀 -> 升入 L2 並觸發 RAM <100ms 清零
    aligner.push_mmwave(
        MmWaveFrame(
            timestamp_ms=1000.0,
            centroid_z_m=0.25,
            vz_mps=-1.4,
            micro_doppler_human_score=0.88,
            breathing_bpm=6.0,
            breathing_confidence=0.2,
        )
    )
    aligner.push_thermal(
        ThermalFrame(timestamp_ms=1000.0, max_temp_c=36.5, human_body_temp_ratio=0.55, grid_flat=[36.5] * 192)
    )
    al = aligner.align(1000.0)
    ds = ds_engine.fuse(al, ScenarioId.A1, fsm.current_level)
    ev = mod.evaluate(al, ds)
    fsm.step(al, ds, ev, override_sim_time_s=1.0)
    assert fsm.current_level == PrivacyLevel.L2
    assert fsm.last_ram_wipe_ms > 0.0
    assert fsm.last_ram_wipe_ms < 100.0
    assert fsm.zero_disk_persistence is True

    # Step 4: 測試高算力負載下的 Graceful Degradation（RGB 降為 10 fps，保雷達 20 Hz）
    fsm.set_burst_load(True)
    fsm.step(al, ds, ev, override_sim_time_s=1.2)
    assert fsm.graceful_degradation_active is True
    sensors_deg, _, _, lat_deg = fsm.compute_metrics()
    assert sensors_deg.rgb_fps == 10.0
    assert sensors_deg.mmwave_hz == 20.0
    assert lat_deg.total_e2e_ms < 500.0
    fsm.set_burst_load(False)

    # Step 5: 連續 3 幀 CRITICAL 觸發進入 L3
    fsm.step(al, ds, ev, override_sim_time_s=1.4)
    assert fsm.current_level == PrivacyLevel.L3

    # Step 6: 恢復空場安全情境，驗證 15 秒時仍受防抖鎖定於 L3，滿 35 秒後安全降階至 L0
    aligner.clear()
    aligner.push_csi(CsiFrame(timestamp_ms=2000.0, fresnel_variance=0.04))
    al_calm = aligner.align(2000.0)
    ds_calm = ds_engine.fuse(al_calm, ScenarioId.A1, fsm.current_level)
    ev_calm = mod.evaluate(al_calm, ds_calm)

    fsm.step(al_calm, ds_calm, ev_calm, override_sim_time_s=10.0)
    fsm.step(al_calm, ds_calm, ev_calm, override_sim_time_s=25.0)
    assert fsm.current_level == PrivacyLevel.L3  # 尚未滿 35s 防抖

    fsm.step(al_calm, ds_calm, ev_calm, override_sim_time_s=46.0)
    assert fsm.current_level == PrivacyLevel.L0  # 滿 35s 安全降階


# ---------------------------------------------------------------------------
# 4. 五大場景模組（A1 / C4 / B1 / C1 / B2）真陽性與誤報過濾驗證
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tp_event,expected_scenario,expected_protocol",
    [
        ("a1_bathroom_fall", ScenarioId.A1, "MQTT / SIP"),
        ("c4_thermal_runaway_smoke", ScenarioId.C4, "Modbus TCP"),
        ("b1_perimeter_intrusion", ScenarioId.B1, "ONVIF"),
        ("c1_hrc_collision_risk", ScenarioId.C1, "PROFINET"),
    ],
)
def test_scenarios_true_positives(tp_event: str, expected_scenario: ScenarioId, expected_protocol: str) -> None:
    """驗證 A1, C4, B1, C1 四大危險異常情境能準確觸發高風險告警與對應北向協定指令。"""
    fusion_system.inject_event("normal_empty", fast_forward_s=50.0)
    snap = fusion_system.inject_event(tp_event)

    assert snap.active_scenario == expected_scenario
    assert snap.evaluation.triggered is True
    assert snap.evaluation.false_alarm_suppressed is False
    assert snap.evaluation.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
    assert snap.privacy_level in (PrivacyLevel.L2, PrivacyLevel.L3)
    assert len(snap.evaluation.northbound_commands) >= 1
    assert expected_protocol in snap.evaluation.northbound_commands[0]["protocol"]


@pytest.mark.parametrize(
    "noise_event,expected_scenario",
    [
        ("a1_pet_false_alarm", ScenarioId.A1),
        ("b1_sunlight_animal_noise", ScenarioId.B1),
        ("c1_cable_water_mist_noise", ScenarioId.C1),
        ("b2_hvac_glass_passerby", ScenarioId.B2),
    ],
)
def test_scenarios_false_alarm_filtering(noise_event: str, expected_scenario: ScenarioId) -> None:
    """驗證五大場景對寵物/水蒸氣、陽光反光/動物、晃動電纜/水霧及玻璃外行人之抗誤報過濾能力。"""
    fusion_system.inject_event("normal_empty", fast_forward_s=50.0)
    snap = fusion_system.inject_event(noise_event)

    assert snap.active_scenario == expected_scenario
    assert snap.evaluation.triggered is False
    assert snap.evaluation.false_alarm_suppressed is True
    assert snap.evaluation.risk_level == RiskLevel.SAFE
    assert any(flag.triggered for flag in snap.evaluation.anti_false_alarm_flags)


def test_b2_office_occupied_zero_false_off_and_camera_off() -> None:
    """驗證 B2 智慧商辦靜坐員工能觸發 HVAC 維持舒適運轉（零 False-Off）且嚴格維持 L1 不開鏡。"""
    fusion_system.inject_event("normal_empty", fast_forward_s=50.0)
    snap = fusion_system.inject_event("b2_office_occupied")

    assert snap.active_scenario == ScenarioId.B2
    assert snap.evaluation.triggered is True
    assert snap.evaluation.risk_level == RiskLevel.SAFE
    assert snap.privacy_level == PrivacyLevel.L1
    assert snap.sensor_status.rgb_fps == 0.0
    assert snap.evaluation.northbound_commands[0]["action"] == "HVAC_MAINTAIN_COMFORT_ACTIVE"


# ---------------------------------------------------------------------------
# 5. FastAPI REST API 與 WebSocket 端點整合測試
# ---------------------------------------------------------------------------


def test_rest_endpoints_and_hardware_profiles() -> None:
    """完整測試 REST API 端點、硬體切換、L3 雙因子授權與稽核紀錄。"""
    with TestClient(app) as client:
        # 1. 首頁 HTML 無外部依賴驗證
        res_root = client.get("/")
        assert res_root.status_code == 200
        assert "四模態 AI 感測融合" in res_root.text

        # 2. Health & State & Catalog
        res_health = client.get("/api/health")
        assert res_health.status_code == 200
        assert res_health.json()["status"] == "ok"

        res_cat = client.get("/api/scenarios")
        assert res_cat.status_code == 200
        cat = res_cat.json()
        assert len(cat["scenarios"]) == 5
        assert len(cat["hardware_profiles"]) == 3
        assert len(cat["injectable_events"]) == 11

        # 3. 切換三大硬體部署形態並檢驗算力與 PoE 預算
        for pid, expected_tops, expected_poe in [
            ("phase1_distributed_mvp", 6.0, 25.5),
            ("phase2_all_in_one_tier2", 12.0, 51.0),
            ("tier3_industrial", 70.0, 71.3),
        ]:
            r_hw = client.post(f"/api/hardware/{pid}")
            assert r_hw.status_code == 200
            hw = r_hw.json()["hardware_profile"]
            assert hw["id"] == pid
            assert hw["npu_tops"] == expected_tops
            assert hw["poe_budget_w"] == expected_poe

        # 4. 場景熱切換與情境注入
        assert client.post("/api/scenario/C4").status_code == 200
        r_inj = client.post("/api/inject/c4_thermal_runaway_smoke")
        assert r_inj.status_code == 200
        assert r_inj.json()["evaluation"]["triggered"] is True

        # 5. L3 雙因子授權驗證（錯 OTP 應 403，正確 OTP 應 200 並可撤銷）
        r_bad = client.post(
            "/api/l3/authorize",
            json={"operator_id": "op1", "otp_token": "000000", "action": "grant"},
        )
        assert r_bad.status_code == 403

        r_ok = client.post(
            "/api/l3/authorize",
            json={"operator_id": "op1", "otp_token": "842916", "action": "grant"},
        )
        assert r_ok.status_code == 200
        assert r_ok.json()["authorized"] is True
        assert r_ok.json()["state"]["privacy_level"] == "L3"

        r_rev = client.post(
            "/api/l3/authorize",
            json={"operator_id": "op1", "otp_token": "842916", "action": "revoke"},
        )
        assert r_rev.status_code == 200
        assert r_rev.json()["authorized"] is False

        # 6. 稽核日誌查詢與錯誤路徑檢查
        r_aud = client.get("/api/audit?limit=15")
        assert r_aud.status_code == 200
        assert r_aud.json()["count"] > 0
        assert r_aud.json()["zero_disk_persistence"] is True

        assert client.post("/api/scenario/UNKNOWN").status_code == 404
        assert client.post("/api/hardware/UNKNOWN").status_code == 404
        assert client.post("/api/inject/non_existent_event").status_code == 404


def test_websocket_telemetry_stream() -> None:
    """驗證 /ws/telemetry WebSocket 即時推送正常並包含完整四模態快照欄位。"""
    with TestClient(app) as client:
        with client.websocket_connect("/ws/telemetry") as ws:
            msg1 = ws.receive_json()
            msg2 = ws.receive_json()
            for msg in (msg1, msg2):
                assert "privacy_level" in msg
                assert "ds_fusion" in msg
                assert "aligned_frame" in msg
                assert "power" in msg
                assert "bandwidth" in msg
                assert "latency" in msg
            assert msg2["sim_time_s"] >= msg1["sim_time_s"]


# ---------------------------------------------------------------------------
# 6. Log-Replay 公開資料集回放、外部 CSV 轉檔與高層批次評估測試
# ---------------------------------------------------------------------------


def test_replay_tracks_and_batch_evaluation() -> None:
    """驗證 5 條實測時間軸回放控制與高層批次評估報表（單模態誤報大幅削減）。"""
    from app.replay_engine import LogReplayEngine

    eng = LogReplayEngine()
    tracks = eng.list_tracks()
    assert len(tracks) >= 5

    # 測試載入與 seek/步進控制
    a1_track = eng.load_track("a1_elderly_fall_and_pet_replay")
    assert a1_track.scenario_id == ScenarioId.A1
    assert len(a1_track.samples) == 120

    eng.control(playing=True, seek_index=30, speed=1.0)
    s1 = eng.current_sample(advance=True)
    assert s1 is not None
    assert s1.frame_idx == 30
    assert s1.single_modality_would_false_alarm is True

    # 測試全部軌跡批次評估
    rep = eng.evaluate_all_tracks(profile_id=HardwareProfileId.PHASE2_ALL_IN_ONE_TIER2)
    summary = rep["summary"]
    assert summary["tracks_evaluated"] >= 5
    assert summary["total_frames"] >= 600
    assert summary["recall_pct"] >= 95.0
    assert summary["four_modality_false_alarms"] < summary["single_modality_false_alarms"]
    assert summary["far_reduction_pct"] >= 95.0
    assert summary["standby_bandwidth_saving_pct"] >= 99.0

    b2_rep = next(t for t in rep["tracks"] if t["track_id"] == "b2_office_hvac_replay")
    assert b2_rep["b2_occupied_hold_rate_pct"] == pytest.approx(100.0, abs=0.1)


def test_public_csv_importers() -> None:
    """驗證 ESP-CSI / TI mmWave / MLX90640 Thermal 公開格式 CSV 解析器。"""
    from app.replay_engine import parse_esp_csi_csv, parse_thermal_csv, parse_ti_mmwave_csv

    # 1. Espressif ESP-CSI CSV
    esp_csv = (
        "type,id,mac,rssi,timestamp_ms,data\n"
        'CSI_DATA,1,AA:BB:CC:DD:EE:FF,-45,1000,"[12, 8, -10, 15, 22, -6, 18, 9, -4, 14, 7, 19]"\n'
        'CSI_DATA,2,AA:BB:CC:DD:EE:FF,-49,1125,"[40, 28, -32, 44, 52, -16, 48, 22, -14, 34, 17, 41]"\n'
    )
    t_csi = parse_esp_csi_csv(esp_csv, track_id="test_esp", scenario_id=ScenarioId.A1)
    assert len(t_csi.samples) == 2
    assert len(t_csi.samples[1].csi.subcarrier_amplitudes) == 30
    assert t_csi.samples[1].csi.fresnel_variance > 0.0

    # 2. TI mmWave Point Cloud CSV
    ti_csv = (
        "frame,timestamp_ms,x,y,z,vx,vy,vz,snr\n"
        "1,2000,0.1,2.2,1.25,0.0,0.2,0.0,18.5\n"
        "1,2000,0.2,2.3,1.30,0.0,0.2,0.0,17.0\n"
        "2,2125,0.1,2.1,0.35,0.0,0.0,-1.3,19.2\n"
    )
    t_mmw = parse_ti_mmwave_csv(ti_csv, track_id="test_ti", scenario_id=ScenarioId.A1)
    assert len(t_mmw.samples) == 2
    assert len(t_mmw.samples[0].mmwave.points) == 2
    assert t_mmw.samples[1].mmwave.vz_mps == pytest.approx(-1.3, abs=1e-3)

    # 3. MLX90640 32x24 (768 px) Thermal CSV -> 自動下採樣為 16x12 (192 px)
    row1 = ",".join(["25.0"] * 768)
    px_hot = ["26.0"] * 768
    for idx_hot in (68, 69, 100, 101):
        px_hot[idx_hot] = "78.5"
    row2 = ",".join(px_hot)
    thm_csv = f"{row1}\n{row2}\n"
    t_thm = parse_thermal_csv(thm_csv, track_id="test_thm", scenario_id=ScenarioId.C4)
    assert len(t_thm.samples) == 2
    assert len(t_thm.samples[1].thermal.grid_flat) == 192
    assert t_thm.samples[1].thermal.max_temp_c == pytest.approx(78.5, abs=0.1)
    assert t_thm.samples[1].gt_is_alert is True


# ---------------------------------------------------------------------------
# 7. Hybrid Live Webcam 隱私閘門、真實 frame.fill(0) 清零與新 API 測試
# ---------------------------------------------------------------------------


def test_live_camera_privacy_gate_and_ram_wipe() -> None:
    """驗證 L0/L1 硬體門禁拒絕、L2 真實影像骨架提取與 Volatile RAM 全零覆寫。"""
    import cv2
    import numpy as np

    from app.live_camera import LiveCameraPoseEngine

    cam = LiveCameraPoseEngine()

    # 構造一張真實 320x240 BGR 測試影像（非全零像素）
    img_l0 = np.full((240, 320, 3), 140, dtype=np.uint8)
    ok_l0, meta_l0, st_l0 = cam.process_bgr_frame(img_l0, PrivacyLevel.L0, l3_authorized=False)
    assert ok_l0 is False
    assert meta_l0 is None
    assert st_l0.camera_hardware_state == "REJECTED_BY_GATE"
    assert int(img_l0.sum()) == 0  # 即使拒絕亦強制銷毀傳入像素

    ok_l1, meta_l1, _ = cam.process_bgr_frame(
        np.full((240, 320, 3), 120, dtype=np.uint8), PrivacyLevel.L1, l3_authorized=False
    )
    assert ok_l1 is False
    assert meta_l1 is None

    # L2 模式：允許在 RAM 解碼與推論，推論完立刻全零清零且耗時 < 100ms
    img_l2 = np.full((240, 320, 3), 85, dtype=np.uint8)
    cv2.rectangle(img_l2, (110, 40), (210, 220), (220, 220, 220), -1)
    ok_enc, enc_buf = cv2.imencode(".jpg", img_l2)
    assert ok_enc is True

    ok_l2, meta_l2, st_l2 = cam.process_uploaded_jpeg(
        enc_buf.tobytes(), PrivacyLevel.L2, l3_authorized=False
    )
    assert ok_l2 is True
    assert meta_l2 is not None
    assert meta_l2.captured_in_ram_only is True
    assert meta_l2.raw_frame_wiped is True
    assert meta_l2.ram_wipe_duration_ms < 100.0
    assert st_l2.camera_hardware_state == "L2_EPHEMERAL_RAM"
    assert st_l2.l3_preview_b64 is None

    # L3 且已雙因子授權：才允許產生遮罩浮水印預覽
    ok_l3, meta_l3, st_l3 = cam.process_uploaded_jpeg(
        enc_buf.tobytes(), PrivacyLevel.L3, l3_authorized=True
    )
    assert ok_l3 is True
    assert meta_l3 is not None and meta_l3.encrypted_forensic_ready is True
    assert st_l3.l3_preview_b64 is not None

    # 強制退回 L0 後立刻清除預覽並切為 PHYSICALLY_OFF
    st_off = cam.enforce_privacy_gate(PrivacyLevel.L0)
    assert st_off.camera_hardware_state == "PHYSICALLY_OFF"
    assert st_off.l3_preview_b64 is None


def test_hybrid_live_and_replay_rest_endpoints() -> None:
    """驗證 /api/mode/*、/api/replay/* 與 /api/camera/* 所有新增端點及狀態復歸。"""
    import cv2
    import numpy as np

    uploaded_dir = PROJECT_ROOT / "datasets" / "uploaded"

    with TestClient(app) as client:
        # 1. 列出回放軌跡
        r_tracks = client.get("/api/replay/tracks")
        assert r_tracks.status_code == 200
        assert len(r_tracks.json()["tracks"]) >= 5

        # 2. 載入 C4 回放軌跡並拖曳至第 65 幀（熱失控段）
        r_load = client.post("/api/replay/load/c4_bess_runaway_smoke_replay")
        assert r_load.status_code == 200
        assert r_load.json()["source_mode"] == "log_replay"
        assert r_load.json()["active_scenario"] == "C4"

        r_seek = client.post("/api/replay/control", json={"playing": False, "seek_index": 65, "step_once": True})
        assert r_seek.status_code == 200
        s_data = r_seek.json()
        assert s_data["replay_status"]["gt_is_alert"] is True
        assert s_data["evaluation"]["triggered"] is True

        # 3. 切至混合實況並驗證 L0 拒絕與 L2 接收
        assert client.post("/api/mode/hybrid_live").status_code == 200
        # 切回安全軌跡首幀（L0）
        client.post("/api/replay/load/a1_elderly_fall_and_pet_replay")
        client.post("/api/mode/hybrid_live")

        dummy_bgr = np.full((120, 160, 3), 100, dtype=np.uint8)
        _, jpg = cv2.imencode(".jpg", dummy_bgr)
        jpg_bytes = jpg.tobytes()

        r_cam_l0 = client.post(
            "/api/camera/frame",
            files={"file": ("test.jpg", jpg_bytes, "image/jpeg")},
        )
        assert r_cam_l0.status_code == 200
        assert r_cam_l0.json()["accepted"] is False

        # 切至跌倒段觸發高風險 L2/L3 後再送畫面，應獲接受並於 RAM 清零
        client.post("/api/replay/control", json={"seek_index": 62, "step_once": True})
        r_cam_l2 = client.post(
            "/api/camera/frame",
            files={"file": ("test.jpg", jpg_bytes, "image/jpeg")},
        )
        assert r_cam_l2.status_code == 200
        assert r_cam_l2.json()["accepted"] is True
        assert r_cam_l2.json()["metadata"]["raw_frame_wiped"] is True

        # 4. 上傳外部 TI mmWave CSV 檔案並測試評估 API
        ti_csv = b"frame,timestamp_ms,x,y,z,vx,vy,vz,snr\n1,100,0.0,2.0,1.2,0.0,0.2,0.0,18\n"
        r_up = client.post(
            "/api/replay/upload",
            files={"file": ("pytest_temp_mmw.csv", ti_csv, "text/csv")},
            data={"fmt": "ti_mmwave", "scenario_id": "A1"},
        )
        assert r_up.status_code == 200
        assert r_up.json()["track"]["track_id"] == "up_pytest_temp_mmw"

        r_eval = client.post("/api/replay/evaluate?track_id=a1_elderly_fall_and_pet_replay")
        assert r_eval.status_code == 200
        assert r_eval.json()["tracks"][0]["far_reduction_pct"] == 100.0

        # 清理測試產生的暫存上傳檔案與恢復模擬器狀態
        temp_up = uploaded_dir / "up_pytest_temp_mmw.jsonl"
        if temp_up.exists():
            temp_up.unlink()
        fusion_system.replay_engine.reload_tracks()
        client.post("/api/mode/simulator")
        client.post("/api/inject/normal_empty")


def test_executive_demo_endpoints_and_blind_detection() -> None:
    """驗證 /demo 高層互動簡報頁、真實 GitHub ESP32 資料集端點與遮鏡失明實測。"""
    import cv2
    import numpy as np

    with TestClient(app) as client:
        # 1. /demo HTML 頁面
        r_demo = client.get("/demo")
        assert r_demo.status_code == 200
        assert "高層 3 分鐘互動實測 Demo" in r_demo.text

        # 2. 官方真實 ESP32 CSI CSV 資料集端點
        r_csi = client.get("/api/demo/real-csi")
        assert r_csi.status_code == 200
        csi_data = r_csi.json()
        assert csi_data["available"] is True
        assert csi_data["total_packets"] == 917
        assert len(csi_data["frames"]) > 0
        assert any("CSI_DATA" in ln for ln in csi_data["raw_preview_lines"])

        # 3. 清晰圖片測試（應為 non-blind、YOLOv8n-pose 後端、RAM 清零且磁碟零寫入）
        clear_img = np.full((240, 320, 3), 135, dtype=np.uint8)
        for k in range(0, 320, 16):
            cv2.line(clear_img, (k, 0), (k, 240), (220, 220, 220), 2)
        _, jpg_clear = cv2.imencode(".jpg", clear_img)
        r_clear = client.post(
            "/api/demo/pose",
            files={"file": ("clear.jpg", jpg_clear.tobytes(), "image/jpeg")},
        )
        assert r_clear.status_code == 200
        d_clear = r_clear.json()
        assert d_clear["ok"] is True
        assert d_clear["pose_backend"] in ("yolov8n_pose_onnx", "yolov8n_pose_local")
        assert d_clear["visual_blind"] is False
        assert d_clear["zero_verified"] is True
        assert d_clear["ram_wipe_ms"] < 100.0
        assert d_clear["disk_files_written"] == 0

        # 4. 全黑遮鏡測試（應偵測為 visual_blind=True）
        dark_img = np.full((240, 320, 3), 5, dtype=np.uint8)
        _, jpg_dark = cv2.imencode(".jpg", dark_img)
        r_dark = client.post(
            "/api/demo/pose",
            files={"file": ("dark.jpg", jpg_dark.tobytes(), "image/jpeg")},
        )
        assert r_dark.status_code == 200
        d_dark = r_dark.json()
        assert d_dark["ok"] is True
        assert d_dark["visual_blind"] is True
        assert d_dark["brightness"] < 28.0
        assert d_dark["zero_verified"] is True
        assert d_dark["disk_files_written"] == 0
