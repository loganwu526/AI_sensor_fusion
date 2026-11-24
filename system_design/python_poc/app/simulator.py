"""多模態感測模擬器與全景融合管線協調器（Simulator & Orchestrator）。

負責模擬不同採樣頻率之四模態異步感測幀（100–1000 Hz CSI、10–20 Hz mmWave、
9–30 Hz Thermal、30 fps RGB 17 點骨架），支援預設運行與 11 種標準／真異常／
故意誤報干擾測試情境之即時注入，並串聯：
  SlidingWindowAligner (100ms) -> DempsterShaferFusionEngine -> ScenarioModule -> PrivacyStateMachine
"""

from __future__ import annotations

import math
import random
import threading
from typing import Optional

from .fusion_engine import DempsterShaferFusionEngine, SlidingWindowAligner
from .live_camera import LiveCameraPoseEngine
from .models import (
    CsiFrame,
    HardwareProfile,
    HardwareProfileId,
    LiveCameraStatus,
    MmWaveFrame,
    MmWavePoint,
    PrivacyLevel,
    ReplayStatus,
    RgbFrameMetadata,
    ScenarioId,
    SkeletonKeypoint,
    SourceMode,
    SystemStateSnapshot,
    ThermalFrame,
    TrackSummary,
)
from .modules import ScenarioModule, create_scenario_registry
from .replay_engine import LogReplayEngine, ReplaySample
from .state_machine import HARDWARE_PROFILES, PRIVACY_COMPLIANCE_DESC, PrivacyStateMachine

COCO_17_NAMES: tuple[str, ...] = (
    "nose",
    "left_eye",
    "right_eye",
    "left_ear",
    "right_ear",
    "left_shoulder",
    "right_shoulder",
    "left_elbow",
    "right_elbow",
    "left_wrist",
    "right_wrist",
    "left_hip",
    "right_hip",
    "left_knee",
    "right_knee",
    "left_ankle",
    "right_ankle",
)

INJECTABLE_EVENTS: dict[str, dict] = {
    "normal_empty": {
        "title": "日常無人待機 (L0 節能巡檢)",
        "category": "normal",
        "target_scenario": None,
        "description": "空間無人移動，CSI 通道平穩，mmWave 處於 LPDS 休眠、Thermal 關斷、Camera 待機。",
    },
    "normal_activity": {
        "title": "日常安全活動 (L1 非視覺追蹤)",
        "category": "normal",
        "target_scenario": None,
        "description": "授權人員正常走動或起居，僅由 CSI + mmWave + Thermal 非視覺追蹤，嚴格不開鏡。",
    },
    "a1_bathroom_fall": {
        "title": "A1 衛浴跌倒與呼吸異常 (真陽性)",
        "category": "true_positive",
        "target_scenario": ScenarioId.A1,
        "description": "60GHz 雷達偵測高度急墜與呼吸微弱，Thermal 確認人體熱源，升階 L2 去識別骨架／L3 緊急救護。",
    },
    "a1_pet_false_alarm": {
        "title": "A1 寵物跑跳與水蒸氣干擾 (抗誤報)",
        "category": "false_alarm_noise",
        "target_scenario": ScenarioId.A1,
        "description": "模擬掃地機／寵物跳動與浴室熱水蒸氣，驗證物理高度、微多普勒與瀰散熱斑濾波抑制誤報。",
    },
    "c4_thermal_runaway_smoke": {
        "title": "C4 BESS 熱失控 Stage 1＋濃煙搜救 (真陽性)",
        "category": "true_positive",
        "target_scenario": ScenarioId.C4,
        "description": "儲能櫃電芯溫升斜率破 65–88°C 搶先切斷 BMS，同時濃煙零視距下以 mmWave 定位受困人員。",
    },
    "b1_perimeter_intrusion": {
        "title": "B1 夜間非授權人員翻越周界入侵 (真陽性)",
        "category": "true_positive",
        "target_scenario": ScenarioId.B1,
        "description": "無 BLE/UWB 白名單、mmWave 確認雙足步態逼近且 Thermal 確認人體溫度，觸發周界告警。",
    },
    "b1_sunlight_animal_noise": {
        "title": "B1 陽光反光／野生動物／巡邏白名單干擾 (抗誤報)",
        "category": "false_alarm_noise",
        "target_scenario": ScenarioId.B1,
        "description": "模擬夏季柏油熱浪閃爍、低矮野狗竄過與白名單保全巡邏，驗證零誤報濾波。",
    },
    "c1_hrc_collision_risk": {
        "title": "C1 UWB 跨貨架盲區與機械臂防撞 (真陽性)",
        "category": "true_positive",
        "target_scenario": ScenarioId.C1,
        "description": "UWB 穿貨架提前預警＋mmWave 近距逼近機械臂危險半徑，觸發 ISO/TS 15066 動態降速或急停。",
    },
    "c1_cable_water_mist_noise": {
        "title": "C1 晃動電纜與高壓水霧干擾 (抗誤報)",
        "category": "false_alarm_noise",
        "target_scenario": ScenarioId.C1,
        "description": "模擬廠房垂落電纜與清洗水霧散射，透過 Thermal 排除非活體溫差，消除昂貴產線誤急停。",
    },
    "b2_hvac_glass_passerby": {
        "title": "B2 玻璃隔間外走廊行人＋靜態熱源 (抗誤報)",
        "category": "false_alarm_noise",
        "target_scenario": ScenarioId.B2,
        "description": "排除雷達穿透玻璃偵測走廊路人與咖啡機等靜態發熱電器，安全進入節能待機。",
    },
    "b2_office_occupied": {
        "title": "B2 靜坐辦公呼吸存在確認 (零 False-Off)",
        "category": "normal",
        "target_scenario": ScenarioId.B2,
        "description": "日間嚴格鎖定 L1 Camera-Off，以 mmWave 微弱胸腔呼吸與熱像確認員工在席，消除 PIR 誤關冷氣。",
    },
}


class SensorFusionSystem:
    """整合感測模擬器、滑動視窗對齊、D-S 融合、五大場景與狀態機的系統核心。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.modules: dict[ScenarioId, ScenarioModule] = create_scenario_registry()
        self.active_scenario: ScenarioId = ScenarioId.A1
        self.active_event: str = "normal_empty"
        self.source_mode: SourceMode = SourceMode.SIMULATOR

        self.aligner = SlidingWindowAligner(window_ms=100.0, tolerance_ms=15.0)
        self.ds_engine = DempsterShaferFusionEngine()
        self.fsm = PrivacyStateMachine(
            profile_id=HardwareProfileId.PHASE2_ALL_IN_ONE_TIER2,
            hysteresis_s=35.0,
            critical_confirm_frames=3,
        )
        self.replay_engine = LogReplayEngine()
        self.live_camera = LiveCameraPoseEngine()

        self._tick_idx: int = 0
        self._rng = random.Random(42)
        self._last_snapshot: Optional[SystemStateSnapshot] = None
        self._current_replay_sample: Optional[ReplaySample] = None
        # 執行初始一幀確保系統啟動即具備完整快照
        self.step(dt_s=0.125)

    def _build_thermal_grid(
        self,
        ambient_c: float,
        hotspots: list[tuple[float, float, float, float]],
        noise_std: float = 0.25,
    ) -> list[float]:
        """產生 16×12 偽彩熱成像矩陣（支援多點高斯熱斑）。

        `hotspots` 元素為 `(norm_x, norm_y, delta_temp_c, sigma)`。
        """
        w, h = 16, 12
        grid: list[float] = []
        for row in range(h):
            ny = (row + 0.5) / h
            for col in range(w):
                nx = (col + 0.5) / w
                val = ambient_c + self._rng.gauss(0.0, noise_std)
                for hx, hy, dtemp, sigma in hotspots:
                    dist2 = (nx - hx) ** 2 + (ny - hy) ** 2
                    val += dtemp * math.exp(-dist2 / (2.0 * sigma * sigma))
                grid.append(round(val, 2))
        return grid

    def _build_skeleton_17(self, pose: str, phase: float) -> list[SkeletonKeypoint]:
        """產生 COCO 17 點去識別化火柴人骨架（standing / sitting / fallen）。"""
        sway = 0.015 * math.sin(phase * 0.7)
        if pose == "fallen":
            # 水平倒臥地面姿態
            coords = [
                (0.24, 0.73),  # nose
                (0.23, 0.71), (0.23, 0.75),  # eyes
                (0.26, 0.70), (0.26, 0.76),  # ears
                (0.35, 0.69), (0.35, 0.78),  # shoulders
                (0.42, 0.64), (0.44, 0.83),  # elbows
                (0.49, 0.67), (0.50, 0.85),  # wrists
                (0.55, 0.71), (0.55, 0.77),  # hips
                (0.68, 0.69), (0.67, 0.80),  # knees
                (0.80, 0.71), (0.78, 0.82),  # ankles
            ]
        elif pose == "sitting":
            coords = [
                (0.50 + sway, 0.25),
                (0.48 + sway, 0.23), (0.52 + sway, 0.23),
                (0.46 + sway, 0.24), (0.54 + sway, 0.24),
                (0.42 + sway, 0.36), (0.58 + sway, 0.36),
                (0.38 + sway, 0.49), (0.62 + sway, 0.49),
                (0.43 + sway, 0.56), (0.57 + sway, 0.56),
                (0.44, 0.58), (0.56, 0.58),
                (0.41, 0.72), (0.59, 0.72),
                (0.42, 0.86), (0.58, 0.86),
            ]
        else:  # standing / walking
            stride = 0.035 * math.sin(phase)
            coords = [
                (0.50 + sway, 0.16),
                (0.48 + sway, 0.14), (0.52 + sway, 0.14),
                (0.46 + sway, 0.15), (0.54 + sway, 0.15),
                (0.41 + sway, 0.28), (0.59 + sway, 0.28),
                (0.36 + sway, 0.42 - stride * 0.5), (0.64 + sway, 0.42 + stride * 0.5),
                (0.35 + sway, 0.54 - stride), (0.65 + sway, 0.54 + stride),
                (0.44 + sway, 0.53), (0.56 + sway, 0.53),
                (0.43 - stride * 0.6, 0.71), (0.57 + stride * 0.6, 0.71),
                (0.42 - stride, 0.88), (0.58 + stride, 0.88),
            ]
        return [
            SkeletonKeypoint(
                name=name,
                x=round(max(0.02, min(0.98, x)), 4),
                y=round(max(0.02, min(0.98, y)), 4),
                conf=round(0.88 + 0.09 * self._rng.random(), 3),
            )
            for name, (x, y) in zip(COCO_17_NAMES, coords)
        ]

    def _build_radar_cluster(
        self,
        cx: float,
        cy: float,
        cz: float,
        vx: float,
        vy: float,
        vz: float,
        count: int,
        spread_xy: float = 0.28,
        spread_z: float = 0.35,
        snr_base: float = 18.0,
        rcs_base: float = 0.75,
    ) -> list[MmWavePoint]:
        pts: list[MmWavePoint] = []
        for _ in range(count):
            pts.append(
                MmWavePoint(
                    x=round(cx + self._rng.gauss(0.0, spread_xy), 3),
                    y=round(max(0.2, cy + self._rng.gauss(0.0, spread_xy)), 3),
                    z=round(max(0.05, cz + self._rng.gauss(0.0, spread_z)), 3),
                    vx=round(vx + self._rng.gauss(0.0, 0.06), 3),
                    vy=round(vy + self._rng.gauss(0.0, 0.06), 3),
                    vz=round(vz + self._rng.gauss(0.0, 0.05), 3),
                    snr=round(max(5.0, snr_base + self._rng.gauss(0.0, 2.5)), 2),
                    rcs=round(max(0.05, rcs_base + self._rng.gauss(0.0, 0.1)), 3),
                )
            )
        return pts

    def _generate_frames_for_event(self, target_ms: float) -> None:
        """依據當前 `active_event` 生成具有真實異步偏移之四模態感測幀並推入對齊器。"""
        ev = self.active_event
        phase = self._tick_idx * 0.35

        # 模擬真實硬體非同步時差（皆落於 100ms 視窗內）
        csi_ts = target_ms - 4.0
        mmw_ts = target_ms - 18.0
        thm_ts = target_ms - 28.0
        rgb_ts = target_ms - 8.0

        # 預設空場背景參數
        fresnel_var = 0.06 + 0.03 * abs(math.sin(phase))
        doppler_e = 0.05
        motion = 0.04
        ble_ok = False
        uwb_dist: Optional[float] = None
        uwb_auth = False
        uwb_nlos = False

        mmw_points: list[MmWavePoint] = []
        cz, vz, vh = 0.0, 0.0, 0.0
        breath_bpm, hr_bpm, breath_conf = 0.0, 0.0, 0.0
        human_md = 0.0
        smoke_pen = False
        dist_m = 4.5

        ambient_c = 24.0
        hotspots: list[tuple[float, float, float, float]] = []
        body_ratio = 0.0
        rise_rate = 0.0
        hx, hy = 0.5, 0.5
        static_heat = False
        glass_ref = False

        pose = "none"
        person_vis = False
        fall_ar = 0.4
        smoke_vis = False
        ppe_ok = True
        glare_mask = False

        if ev == "normal_empty":
            pass
        elif ev == "normal_activity":
            fresnel_var = 0.38 + 0.08 * math.sin(phase)
            doppler_e = 0.32
            motion = 0.36
            ble_ok = True
            dist_m = 2.8 + 0.4 * math.sin(phase * 0.4)
            cz, vz, vh = 1.25, 0.02, 0.65
            breath_bpm, hr_bpm, breath_conf = 16.5, 74.0, 0.82
            human_md = 0.82
            mmw_points = self._build_radar_cluster(
                0.4 * math.sin(phase * 0.5), dist_m, cz, 0.25, 0.45, 0.02, count=18
            )
            hx, hy = 0.5 + 0.1 * math.sin(phase * 0.5), 0.48
            hotspots = [(hx, hy, 12.2, 0.14)]
            body_ratio = 0.52
            pose = "standing"
            person_vis = True

        elif ev == "a1_bathroom_fall":
            fresnel_var = 0.84 + 0.06 * math.sin(phase)
            doppler_e = 0.78
            motion = 0.85
            dist_m = 2.2
            cz = 0.24
            vz = -1.45  # 急墜垂直速度
            vh = 0.08
            breath_bpm = 6.2  # 呼吸微弱異常
            hr_bpm = 104.0
            breath_conf = 0.26
            human_md = 0.86
            mmw_points = self._build_radar_cluster(0.2, dist_m, cz, 0.02, 0.02, vz, count=22, spread_z=0.12)
            hx, hy = 0.52, 0.70
            hotspots = [(hx, hy, 12.6, 0.16)]  # 36.6°C 躺地人體熱輪廓
            body_ratio = 0.58
            pose = "fallen"
            person_vis = True
            fall_ar = 1.95

        elif ev == "a1_pet_false_alarm":
            fresnel_var = 0.58 + 0.10 * math.sin(phase * 1.5)
            doppler_e = 0.45
            motion = 0.52
            dist_m = 2.5
            cz = 0.28  # 低矮寵物/掃地機
            vz = 0.05
            vh = 0.75
            breath_bpm, hr_bpm, breath_conf = 0.0, 0.0, 0.12
            human_md = 0.22  # 非人類微多普勒
            mmw_points = self._build_radar_cluster(
                0.7 * math.sin(phase), dist_m, cz, 0.5, 0.2, 0.04, count=8, spread_xy=0.14, spread_z=0.08, rcs_base=0.18
            )
            ambient_c = 29.0
            hx, hy = 0.45, 0.55
            # 瀰散水蒸氣（大 sigma 導致熱斑彌散高溫且非人體比例）
            hotspots = [(0.5, 0.5, 13.5, 0.48)]
            body_ratio = 0.16
            pose = "none"

        elif ev == "c4_thermal_runaway_smoke":
            fresnel_var = 0.76
            doppler_e = 0.65
            motion = 0.70
            dist_m = 3.1
            cz, vz, vh = 0.92, -0.05, 0.15
            breath_bpm, hr_bpm, breath_conf = 26.0, 118.0, 0.65
            human_md = 0.78
            smoke_pen = True
            mmw_points = self._build_radar_cluster(-0.4, dist_m, cz, 0.1, 0.05, -0.02, count=20)
            ambient_c = 31.0
            hx, hy = 0.68, 0.42
            # 電池櫃高溫熱點 (31 + 57 = 88°C) + 受困人員熱點 (36.5°C)
            hotspots = [(hx, hy, 57.5, 0.11), (0.32, 0.55, 5.8, 0.12)]
            body_ratio = 0.35
            rise_rate = 11.4
            pose = "standing"
            person_vis = True
            smoke_vis = True

        elif ev == "b1_perimeter_intrusion":
            fresnel_var = 0.80 + 0.05 * math.sin(phase)
            doppler_e = 0.74
            motion = 0.82
            ble_ok = False
            uwb_auth = False
            dist_m = max(1.4, 5.5 - (self._tick_idx % 12) * 0.25)
            cz, vz, vh = 1.32, 0.02, 1.35
            breath_bpm, hr_bpm, breath_conf = 20.0, 96.0, 0.72
            human_md = 0.88
            mmw_points = self._build_radar_cluster(0.25, dist_m, cz, 0.15, -1.25, 0.02, count=24)
            hx, hy = 0.54, 0.50
            hotspots = [(hx, hy, 12.4, 0.14)]
            body_ratio = 0.55
            pose = "standing"
            person_vis = True

        elif ev == "b1_sunlight_animal_noise":
            fresnel_var = 0.44
            doppler_e = 0.38
            motion = 0.42
            dist_m = 4.2
            cz, vz, vh = 0.36, 0.0, 1.1  # 野狗低質心
            human_md = 0.26
            mmw_points = self._build_radar_cluster(
                0.9 * math.cos(phase), dist_m, cz, 0.9, 0.3, 0.0, count=9, spread_z=0.10, rcs_base=0.22
            )
            ambient_c = 28.5
            hx, hy = 0.78, 0.24
            hotspots = [(hx, hy, 21.0, 0.18)]  # 柏油或太陽反光局部高溫 (49.5°C)
            body_ratio = 0.12
            glass_ref = True
            glare_mask = True

        elif ev == "c1_hrc_collision_risk":
            fresnel_var = 0.86
            doppler_e = 0.80
            motion = 0.88
            uwb_dist = 0.85  # UWB 跨貨架盲區測距 < 1.0m
            uwb_auth = True
            uwb_nlos = True
            dist_m = 0.88
            cz, vz, vh = 1.28, 0.0, 1.15
            breath_bpm, hr_bpm, breath_conf = 19.0, 92.0, 0.76
            human_md = 0.85
            mmw_points = self._build_radar_cluster(0.1, dist_m, cz, 0.05, -1.10, 0.0, count=24)
            hx, hy = 0.48, 0.48
            hotspots = [(hx, hy, 12.3, 0.15)]
            body_ratio = 0.54
            pose = "standing"
            person_vis = True
            ppe_ok = True

        elif ev == "c1_cable_water_mist_noise":
            fresnel_var = 0.52 + 0.12 * math.sin(phase * 2.0)
            doppler_e = 0.48
            motion = 0.45
            uwb_dist = 6.2
            uwb_nlos = False
            dist_m = 1.4
            cz, vz, vh = 1.45, 0.0, 0.35
            human_md = 0.24  # 電纜擺動與水霧非人類微多普勒
            mmw_points = self._build_radar_cluster(
                0.2 * math.sin(phase * 2.2), dist_m, cz, 0.35, 0.05, 0.02, count=14, spread_xy=0.35, rcs_base=0.15
            )
            ambient_c = 22.5
            hx, hy = 0.50, 0.40
            hotspots = [(hx, hy, 2.2, 0.25)]  # 24.7°C 冷水霧，非活體體溫
            body_ratio = 0.06
            pose = "none"

        elif ev == "b2_hvac_glass_passerby":
            fresnel_var = 0.36
            doppler_e = 0.30
            motion = 0.34
            dist_m = 4.6
            cz, vz, vh = 1.30, 0.0, 0.95
            breath_bpm, hr_bpm, breath_conf = 0.0, 0.0, 0.18
            human_md = 0.48
            mmw_points = self._build_radar_cluster(0.8 * math.sin(phase * 0.6), dist_m, cz, 0.7, 0.0, 0.0, count=14)
            ambient_c = 23.5
            hx, hy = 0.86, 0.50
            hotspots = [(hx, hy, 15.0, 0.12)]  # 邊界玻璃反光 / 咖啡機靜態熱源
            body_ratio = 0.12
            static_heat = True
            glass_ref = True

        elif ev == "b2_office_occupied":
            fresnel_var = 0.24 + 0.03 * math.sin(phase)
            doppler_e = 0.16
            motion = 0.20
            ble_ok = True
            dist_m = 2.4
            cz, vz, vh = 0.98, 0.0, 0.04
            breath_bpm = 15.2 + 0.6 * math.sin(phase * 0.3)
            hr_bpm = 69.0
            breath_conf = 0.91
            human_md = 0.76
            mmw_points = self._build_radar_cluster(0.05, dist_m, cz, 0.01, 0.01, 0.0, count=16, spread_xy=0.22)
            ambient_c = 24.0
            hx, hy = 0.50, 0.50
            hotspots = [(hx, hy, 12.1, 0.16)]  # 36.1°C 靜坐員工
            body_ratio = 0.50
            pose = "sitting"
            person_vis = True

        # 生成 CSI 子載波與相位陣列（30 子載波）
        subcarriers = [
            round(
                max(
                    0.05,
                    min(
                        1.0,
                        0.32
                        + 0.45 * fresnel_var * math.sin(0.35 * k + phase) ** 2
                        + self._rng.gauss(0.0, 0.025),
                    ),
                ),
                4,
            )
            for k in range(30)
        ]
        phases = [
            round(math.sin(0.25 * k + phase * 1.1) * (0.2 + 1.8 * fresnel_var), 4)
            for k in range(30)
        ]

        # 先送入兩筆較高頻的 CSI 測試視窗內降維聚合
        self.aligner.push_csi(
            CsiFrame(
                timestamp_ms=csi_ts - 16.0,
                subcarrier_amplitudes=subcarriers,
                phase_diffs=phases,
                fresnel_variance=round(max(0.0, fresnel_var + self._rng.gauss(0.0, 0.02)), 4),
                doppler_energy=round(doppler_e, 4),
                motion_score=round(motion, 4),
                ble_whitelist_present=ble_ok,
                uwb_tag_distance_m=uwb_dist,
                uwb_authorized=uwb_auth,
                uwb_nlos_alert=uwb_nlos,
            )
        )
        self.aligner.push_csi(
            CsiFrame(
                timestamp_ms=csi_ts,
                subcarrier_amplitudes=subcarriers,
                phase_diffs=phases,
                fresnel_variance=round(max(0.0, fresnel_var), 4),
                doppler_energy=round(doppler_e, 4),
                motion_score=round(motion, 4),
                ble_whitelist_present=ble_ok,
                uwb_tag_distance_m=uwb_dist,
                uwb_authorized=uwb_auth,
                uwb_nlos_alert=uwb_nlos,
            )
        )

        self.aligner.push_mmwave(
            MmWaveFrame(
                timestamp_ms=mmw_ts,
                points=mmw_points,
                centroid_z_m=round(cz, 3),
                vz_mps=round(vz, 3),
                horizontal_speed_mps=round(vh, 3),
                breathing_bpm=round(breath_bpm, 2),
                heart_rate_bpm=round(hr_bpm, 1),
                breathing_confidence=round(breath_conf, 3),
                micro_doppler_human_score=round(human_md, 3),
                smoke_penetration_active=smoke_pen,
                target_distance_m=round(dist_m, 3),
            )
        )

        grid = self._build_thermal_grid(ambient_c, hotspots)
        max_t = max(grid)
        min_t = min(grid)
        self.aligner.push_thermal(
            ThermalFrame(
                timestamp_ms=thm_ts,
                width=16,
                height=12,
                grid_flat=grid,
                max_temp_c=round(max_t, 2),
                min_temp_c=round(min_t, 2),
                ambient_temp_c=ambient_c,
                human_body_temp_ratio=round(body_ratio, 3),
                temp_rise_rate_c_per_min=round(rise_rate, 2),
                hotspot_x=round(hx, 3),
                hotspot_y=round(hy, 3),
                is_static_heat_source=static_heat,
                is_glass_boundary_reflection=glass_ref,
            )
        )

        skeleton = self._build_skeleton_17(pose, phase) if pose != "none" else []
        self.aligner.push_rgb(
            RgbFrameMetadata(
                timestamp_ms=rgb_ts,
                captured_in_ram_only=False,
                person_detected=person_vis,
                posture_label=pose,
                fall_aspect_ratio=fall_ar,
                smoke_or_flame_visual=smoke_vis,
                ppe_compliant=ppe_ok,
                vehicle_or_glare_heat_mask=glare_mask,
                skeleton_17pts=skeleton,
                raw_frame_wiped=True,
                ram_wipe_duration_ms=0.0,
                encrypted_forensic_ready=False,
            )
        )

    def _push_replay_frames(self, target_ms: float, advance: bool = True) -> None:
        """從 LogReplayEngine 讀取真實感測幀，保留其相對時差偏移後推入 100ms 對齊器。"""
        sample = self.replay_engine.current_sample(advance=advance)
        if sample is None:
            self._generate_frames_for_event(target_ms)
            return

        self._current_replay_sample = sample
        ref_ts = sample.timestamp_ms
        csi_off = max(-85.0, min(85.0, sample.csi.timestamp_ms - ref_ts))
        mmw_off = max(-85.0, min(85.0, sample.mmwave.timestamp_ms - ref_ts))
        thm_off = max(-85.0, min(85.0, sample.thermal.timestamp_ms - ref_ts))

        self.aligner.push_csi(sample.csi.model_copy(update={"timestamp_ms": target_ms + csi_off}))
        self.aligner.push_mmwave(sample.mmwave.model_copy(update={"timestamp_ms": target_ms + mmw_off}))
        self.aligner.push_thermal(sample.thermal.model_copy(update={"timestamp_ms": target_ms + thm_off}))

        # 在混合實況模式下，L2/L3 優先採用真實攝影機提取之去識別 17 點骨架
        rgb_meta: Optional[RgbFrameMetadata] = None
        if self.source_mode == SourceMode.HYBRID_LIVE:
            self.live_camera.enforce_privacy_gate(self.fsm.current_level)
            if self.fsm.current_level in (PrivacyLevel.L2, PrivacyLevel.L3):
                if self.live_camera.camera_source == "local_opencv" and self.live_camera.enabled:
                    _, live_meta, _ = self.live_camera.capture_local_webcam(
                        privacy_level=self.fsm.current_level,
                        l3_authorized=self.fsm.l3_authorized,
                        timestamp_ms=target_ms - 6.0,
                    )
                    rgb_meta = live_meta
                elif self.live_camera.latest_metadata is not None:
                    rgb_meta = self.live_camera.latest_metadata.model_copy(
                        update={"timestamp_ms": target_ms - 6.0}
                    )

        if rgb_meta is None:
            rgb_meta = (
                sample.rgb_hint.model_copy(update={"timestamp_ms": target_ms - 6.0})
                if sample.rgb_hint is not None
                else RgbFrameMetadata(timestamp_ms=target_ms - 6.0)
            )
        self.aligner.push_rgb(rgb_meta)

    def _build_replay_status(self) -> Optional[ReplayStatus]:
        """產生與目前步進樣本完全同步的 ReplayStatus。"""
        track = self.replay_engine.get_active_track()
        if track is None or not track.samples:
            return None
        s = self._current_replay_sample or track.samples[
            max(0, min(len(track.samples) - 1, self.replay_engine.cursor))
        ]
        n = len(track.samples)
        pct = round(((s.frame_idx + 1) / n) * 100.0, 1)
        return ReplayStatus(
            active_track_id=track.track_id,
            track_title=track.title,
            dataset_source=track.dataset_source,
            frame_index=s.frame_idx,
            total_frames=n,
            progress_pct=pct,
            playing=self.replay_engine.playing,
            speed=self.replay_engine.speed,
            loop=self.replay_engine.loop,
            gt_segment=s.gt_segment,
            gt_is_alert=s.gt_is_alert,
            single_modality_would_false_alarm=s.single_modality_would_false_alarm,
            single_modality_note=s.single_modality_note,
        )

    def step(
        self,
        dt_s: float = 0.125,
        override_sim_time_s: Optional[float] = None,
        advance_replay: bool = True,
    ) -> SystemStateSnapshot:
        """推演一個時間步，執行對齊、D-S 融合、場景評估與狀態機遷移。"""
        with self._lock:
            self._tick_idx += 1
            sim_s = override_sim_time_s if override_sim_time_s is not None else self.fsm.sim_time_s + dt_s
            target_ms = sim_s * 1000.0

            if self.source_mode == SourceMode.SIMULATOR:
                self._generate_frames_for_event(target_ms)
                if self.live_camera.enabled:
                    self.live_camera.enforce_privacy_gate(self.fsm.current_level)
            else:
                self._push_replay_frames(target_ms, advance=advance_replay)

            aligned = self.aligner.align(target_ms)

            module = self.modules[self.active_scenario]
            # 先以目前狀態等級執行融合計算與模組評估
            ds_result = self.ds_engine.fuse(aligned, self.active_scenario, self.fsm.current_level)
            evaluation = module.evaluate(aligned, ds_result)

            # 若剛由狀態機預判需要升階至 L2/L3，再執行一次含對應活躍模態之融合更新
            hyst_info = self.fsm.step(
                aligned=aligned,
                ds_result=ds_result,
                evaluation=evaluation,
                dt_s=dt_s,
                override_sim_time_s=override_sim_time_s,
            )
            if self.source_mode == SourceMode.HYBRID_LIVE:
                self.live_camera.enforce_privacy_gate(self.fsm.current_level)

            if self.fsm.current_level in (PrivacyLevel.L2, PrivacyLevel.L3):
                ds_result = self.ds_engine.fuse(aligned, self.active_scenario, self.fsm.current_level)
                evaluation = module.evaluate(aligned, ds_result)

            sensors, bw, pwr, lat = self.fsm.compute_metrics()
            effective_wipe_ms = self.fsm.last_ram_wipe_ms
            if (
                self.source_mode == SourceMode.HYBRID_LIVE
                and self.fsm.current_level in (PrivacyLevel.L2, PrivacyLevel.L3)
                and self.live_camera.last_live_wipe_ms > 0.0
            ):
                effective_wipe_ms = self.live_camera.last_live_wipe_ms
                if aligned.rgb is not None:
                    aligned.rgb.ram_wipe_duration_ms = effective_wipe_ms

            snapshot = SystemStateSnapshot(
                sim_time_s=round(self.fsm.sim_time_s, 3),
                source_mode=self.source_mode,
                active_event=self.active_event,
                active_scenario=self.active_scenario,
                hardware_profile=self.fsm.profile,
                privacy_level=self.fsm.current_level,
                privacy_compliance_tier=PRIVACY_COMPLIANCE_DESC[self.fsm.current_level],
                hysteresis_target_level=hyst_info["hysteresis_target_level"],
                hysteresis_elapsed_s=hyst_info["hysteresis_elapsed_s"],
                hysteresis_required_s=self.fsm.hysteresis_required_s,
                graceful_degradation_active=self.fsm.graceful_degradation_active,
                npu_load_pct=self.fsm.npu_load_pct,
                l3_authorized=self.fsm.l3_authorized,
                l3_operator_id=self.fsm.l3_operator_id,
                sensor_status=sensors,
                aligned_frame=aligned,
                ds_fusion=ds_result,
                evaluation=evaluation,
                bandwidth=bw,
                power=pwr,
                latency=lat,
                last_ram_wipe_ms=effective_wipe_ms,
                zero_disk_persistence=self.fsm.zero_disk_persistence,
                recent_audits=self.fsm.get_audits(25),
                replay_status=self._build_replay_status(),
                live_camera_status=self.live_camera.get_status(),
            )
            self._last_snapshot = snapshot
            return snapshot

    def get_snapshot(self) -> SystemStateSnapshot:
        """取得最新系統狀態快照。"""
        with self._lock:
            if self._last_snapshot is None:
                return self.step(dt_s=0.125)
            return self._last_snapshot

    def switch_scenario(self, scenario_id: ScenarioId) -> SystemStateSnapshot:
        """熱切換當前啟用的場景模組。"""
        with self._lock:
            self.active_scenario = scenario_id
            # 若當前為其他場景專屬測試事件，切換為標準對應基線
            ev_meta = INJECTABLE_EVENTS.get(self.active_event, {})
            if ev_meta.get("target_scenario") not in (None, scenario_id):
                self.active_event = "normal_empty"
            self.fsm._record_audit(
                event_type="SCENARIO_HOT_SWAP",
                scenario_id=scenario_id.value,
                detail=f"熱切換場景插件模組為：{self.modules[scenario_id].name}",
            )
            return self.step(dt_s=0.125)

    def switch_hardware(self, profile_id: HardwareProfileId) -> SystemStateSnapshot:
        """動態切換三大硬體部署形態設定。"""
        with self._lock:
            self.fsm.set_hardware_profile(profile_id)
            return self.step(dt_s=0.125)

    def inject_event(
        self,
        event_name: str,
        fast_forward_s: Optional[float] = None,
    ) -> SystemStateSnapshot:
        """注入指定測試情境（正常／真異常／故意誤報干擾），可選快進模擬時間測試防抖。"""
        with self._lock:
            if event_name not in INJECTABLE_EVENTS:
                raise KeyError(f"未知的模擬情境事件：{event_name}")
            meta = INJECTABLE_EVENTS[event_name]
            self.source_mode = SourceMode.SIMULATOR
            self.live_camera.set_enabled(False)
            self.active_event = event_name
            if meta["target_scenario"] is not None:
                self.active_scenario = meta["target_scenario"]
            self.fsm._record_audit(
                event_type="EVENT_INJECT",
                scenario_id=self.active_scenario.value,
                detail=f"注入測試情境 [{event_name}]：{meta['title']}",
            )
            dt = fast_forward_s if fast_forward_s is not None else 0.125
            return self.step(dt_s=dt)

    def fast_forward(self, seconds: float) -> SystemStateSnapshot:
        """快進模擬時間（用於即時測試 L3/L2/L1 遲滯防抖降級）。"""
        with self._lock:
            return self.step(dt_s=max(0.0, seconds))

    def set_burst_stress(self, enabled: bool) -> SystemStateSnapshot:
        """切換高算力壓力負載模式以觸發或解除 Graceful Degradation。"""
        with self._lock:
            self.fsm.set_burst_load(enabled)
            return self.step(dt_s=0.125)

    def authorize_l3(
        self,
        operator_id: str,
        otp_token: str,
        action: str = "grant",
        reason: str = "雙因子人工覆核",
    ) -> tuple[bool, str, SystemStateSnapshot]:
        """模擬 L3 雙因子授權取得加密存證或解除警報。"""
        with self._lock:
            ok, msg = self.fsm.authorize_l3(
                operator_id=operator_id,
                otp_token=otp_token,
                action=action,
                reason=reason,
                scenario_id=self.active_scenario.value,
            )
            snap = self.step(dt_s=0.125)
            return ok, msg, snap

    def set_source_mode(self, mode: SourceMode) -> SystemStateSnapshot:
        """切換資料來源模式（simulator / log_replay / hybrid_live）。"""
        with self._lock:
            self.source_mode = mode
            self.aligner.clear()
            self.fsm._consecutive_critical_frames = 0
            if mode == SourceMode.HYBRID_LIVE:
                self.live_camera.set_enabled(True, source="browser_getusermedia")
                self.live_camera.enforce_privacy_gate(self.fsm.current_level)
                track = self.replay_engine.get_active_track()
                if track is not None:
                    self.active_scenario = track.scenario_id
            elif mode == SourceMode.LOG_REPLAY:
                self.live_camera.set_enabled(False)
                track = self.replay_engine.get_active_track()
                if track is not None:
                    self.active_scenario = track.scenario_id
            else:
                self.live_camera.set_enabled(False)

            self.fsm._record_audit(
                event_type="SOURCE_MODE_SWITCH",
                scenario_id=self.active_scenario.value,
                detail=f"資料來源切換為：{mode.value}",
            )
            return self.step(dt_s=0.125, advance_replay=False)

    def load_replay_track(self, track_id: str) -> SystemStateSnapshot:
        """載入指定時間軸回放資料集並對齊場景。"""
        with self._lock:
            track = self.replay_engine.load_track(track_id)
            self.aligner.clear()
            self.fsm.current_level = PrivacyLevel.L0
            self.fsm.l3_authorized = False
            self.fsm.l3_operator_id = None
            self.fsm._consecutive_critical_frames = 0
            self.fsm._downgrade_target = None
            self.fsm._downgrade_start_s = None
            self.active_scenario = track.scenario_id
            if self.source_mode == SourceMode.SIMULATOR:
                self.source_mode = SourceMode.LOG_REPLAY
            self.fsm._record_audit(
                event_type="REPLAY_TRACK_LOADED",
                scenario_id=track.scenario_id.value,
                detail=f"載入實測時間軸：{track.title}（{len(track.samples)} 幀）",
            )
            return self.step(dt_s=0.125, advance_replay=False)

    def control_replay(
        self,
        playing: Optional[bool] = None,
        seek_index: Optional[int] = None,
        speed: Optional[float] = None,
        loop: Optional[bool] = None,
        step_once: bool = False,
    ) -> SystemStateSnapshot:
        """控制回放播放／暫停／拖曳進度／倍速或單步推進。"""
        with self._lock:
            self.replay_engine.control(
                playing=playing,
                seek_index=seek_index,
                speed=speed,
                loop=loop,
            )
            if seek_index is not None:
                self.aligner.clear()
                self.fsm._consecutive_critical_frames = 0
            if step_once:
                was_playing = self.replay_engine.playing
                self.replay_engine.playing = True
                snap = self.step(dt_s=0.125, advance_replay=True)
                self.replay_engine.playing = was_playing
                return snap
            return self.step(dt_s=0.125, advance_replay=False)

    def upload_replay_dataset(
        self,
        filename: str,
        content_bytes: bytes,
        fmt: str = "auto",
        scenario_id: ScenarioId = ScenarioId.A1,
    ) -> tuple[TrackSummary, SystemStateSnapshot]:
        """匯入外部公開感測資料集並立刻切換載入。"""
        with self._lock:
            track = self.replay_engine.import_dataset(
                filename=filename,
                content_bytes=content_bytes,
                fmt=fmt,
                scenario_id=scenario_id,
                persist=True,
            )
            snap = self.load_replay_track(track.track_id)
            summary = TrackSummary(
                track_id=track.track_id,
                title=track.title,
                scenario_id=track.scenario_id,
                dataset_source=track.dataset_source,
                description=track.description,
                total_frames=len(track.samples),
                fps_nominal=track.fps_nominal,
                is_uploaded=track.is_uploaded,
            )
            return summary, snap

    def evaluate_replay(self, track_id: Optional[str] = None) -> dict:
        """執行單軌或全軌跡高層批次評估報表（無狀態汙染）。"""
        if track_id:
            rep = self.replay_engine.evaluate_track(track_id, profile_id=self.fsm.profile.id)
            return {"summary": None, "tracks": [rep.model_dump()]}
        return self.replay_engine.evaluate_all_tracks(profile_id=self.fsm.profile.id)

    def ingest_live_jpeg(self, jpeg_bytes: bytes, source_name: str = "browser_getusermedia") -> dict:
        """處理來自瀏覽器 getUserMedia 或外部端點之真實 JPEG 畫面，嚴格套用 L0/L1 門禁。"""
        with self._lock:
            self.live_camera.set_enabled(True, source=source_name)
            accepted, meta, cam_status = self.live_camera.process_uploaded_jpeg(
                jpeg_bytes=jpeg_bytes,
                privacy_level=self.fsm.current_level,
                l3_authorized=self.fsm.l3_authorized,
                timestamp_ms=self.fsm.sim_time_s * 1000.0,
            )
            if not accepted:
                self.fsm._record_audit(
                    event_type="PRIVACY_GATE_REJECTED",
                    scenario_id=self.active_scenario.value,
                    detail=cam_status.note,
                )
            else:
                self.fsm.last_ram_wipe_ms = cam_status.last_live_wipe_ms
            snap = self.step(dt_s=0.05, advance_replay=False)
            return {
                "accepted": accepted,
                "live_camera_status": cam_status.model_dump(),
                "metadata": meta.model_dump() if meta else None,
                "state": snap.model_dump(mode="json"),
            }

    def list_catalog(self) -> dict:
        """回傳支援的五大場景模組、三大硬體形態與 11 種可注入情境目錄。"""
        return {
            "source_mode": self.source_mode.value,
            "active_scenario": self.active_scenario.value,
            "active_event": self.active_event,
            "active_hardware": self.fsm.profile.id.value,
            "replay_tracks": [t.model_dump() for t in self.replay_engine.list_tracks()],
            "scenarios": [
                {
                    "id": sid.value,
                    "name": mod.name,
                    "description": mod.description,
                    "sensor_weights": mod.sensor_weights,
                }
                for sid, mod in self.modules.items()
            ],
            "hardware_profiles": [
                p.model_dump() for p in HARDWARE_PROFILES.values()
            ],
            "injectable_events": [
                {
                    "event_name": key,
                    "title": val["title"],
                    "category": val["category"],
                    "target_scenario": val["target_scenario"].value if val["target_scenario"] else None,
                    "description": val["description"],
                }
                for key, val in INJECTABLE_EVENTS.items()
            ],
        }
