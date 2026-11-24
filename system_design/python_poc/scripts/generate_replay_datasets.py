"""產生五大旗艦應用場景（A1, C4, B1, C1, B2）之高擬真多模態時間軸回放資料集（JSONL）。

每條軌跡模擬真實感測訊號之時變波動、高斯測量雜訊、非同步硬體時間戳偏移（落在 100ms 視窗內），
並標註分段 Ground Truth（`gt_segment`, `gt_is_alert`, `gt_occupied`）與單模態基線是否誤報，
供 Log-Replay 與高層批次評估報表直接使用。
"""

from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models import (
    CsiFrame,
    MmWaveFrame,
    MmWavePoint,
    RgbFrameMetadata,
    ScenarioId,
    SkeletonKeypoint,
    ThermalFrame,
)
from app.replay_engine import ReplaySample

COCO_17_NAMES: tuple[str, ...] = (
    "nose", "left_eye", "right_eye", "left_ear", "right_ear",
    "left_shoulder", "right_shoulder", "left_elbow", "right_elbow",
    "left_wrist", "right_wrist", "left_hip", "right_hip",
    "left_knee", "right_knee", "left_ankle", "right_ankle",
)


def _make_csi(
    ts_ms: float,
    rng: random.Random,
    phase: float,
    fresnel: float,
    doppler: float,
    motion: float,
    ble_ok: bool = False,
    uwb_dist: Optional[float] = None,
    uwb_auth: bool = False,
    uwb_nlos: bool = False,
) -> CsiFrame:
    subcarriers = [
        round(
            max(
                0.05,
                min(
                    1.0,
                    0.30 + 0.48 * fresnel * math.sin(0.35 * k + phase) ** 2 + rng.gauss(0.0, 0.022),
                ),
            ),
            4,
        )
        for k in range(30)
    ]
    phases = [
        round(math.sin(0.25 * k + phase * 1.1) * (0.2 + 1.7 * fresnel) + rng.gauss(0.0, 0.04), 4)
        for k in range(30)
    ]
    return CsiFrame(
        timestamp_ms=round(ts_ms, 2),
        subcarrier_amplitudes=subcarriers,
        phase_diffs=phases,
        fresnel_variance=round(max(0.01, fresnel + rng.gauss(0.0, 0.015)), 4),
        doppler_energy=round(max(0.01, doppler + rng.gauss(0.0, 0.012)), 4),
        motion_score=round(max(0.01, min(1.0, motion + rng.gauss(0.0, 0.015))), 4),
        ble_whitelist_present=ble_ok,
        uwb_tag_distance_m=uwb_dist,
        uwb_authorized=uwb_auth,
        uwb_nlos_alert=uwb_nlos,
    )


def _make_points(
    rng: random.Random,
    cx: float,
    cy: float,
    cz: float,
    vx: float,
    vy: float,
    vz: float,
    count: int = 16,
    spread_xy: float = 0.18,
    spread_z: float = 0.25,
    rcs: float = 0.65,
) -> list[MmWavePoint]:
    return [
        MmWavePoint(
            x=round(cx + rng.gauss(0.0, spread_xy), 3),
            y=round(max(0.2, cy + rng.gauss(0.0, spread_xy)), 3),
            z=round(max(0.04, cz + rng.gauss(0.0, spread_z)), 3),
            vx=round(vx + rng.gauss(0.0, 0.04), 3),
            vy=round(vy + rng.gauss(0.0, 0.04), 3),
            vz=round(vz + rng.gauss(0.0, 0.04), 3),
            snr=round(14.0 + rng.uniform(1.0, 7.0), 1),
            rcs=round(max(0.05, rcs + rng.gauss(0.0, 0.05)), 2),
        )
        for _ in range(count)
    ]


def _make_thermal_grid(
    rng: random.Random,
    ambient_c: float,
    hotspots: list[tuple[float, float, float, float]],
) -> list[float]:
    grid: list[float] = []
    for row in range(12):
        ny = (row + 0.5) / 12.0
        for col in range(16):
            nx = (col + 0.5) / 16.0
            val = ambient_c + rng.gauss(0.0, 0.22)
            for hx, hy, dt_c, sigma in hotspots:
                d2 = (nx - hx) ** 2 + (ny - hy) ** 2
                val += dt_c * math.exp(-d2 / (2.0 * sigma * sigma))
            grid.append(round(val, 2))
    return grid


def _make_skeleton(pose: str, phase: float) -> list[SkeletonKeypoint]:
    if pose == "none":
        return []
    sway = 0.012 * math.sin(phase * 0.7)
    if pose == "fallen":
        coords = [
            (0.24, 0.73), (0.23, 0.71), (0.23, 0.75), (0.26, 0.70), (0.26, 0.76),
            (0.35, 0.69), (0.35, 0.78), (0.42, 0.64), (0.44, 0.83),
            (0.49, 0.67), (0.50, 0.85), (0.56, 0.71), (0.56, 0.77),
            (0.70, 0.69), (0.71, 0.80), (0.83, 0.72), (0.85, 0.82),
        ]
    elif pose == "sitting":
        coords = [
            (0.50 + sway, 0.26), (0.48 + sway, 0.24), (0.52 + sway, 0.24),
            (0.46 + sway, 0.26), (0.54 + sway, 0.26),
            (0.42 + sway, 0.40), (0.58 + sway, 0.40),
            (0.39 + sway, 0.54), (0.61 + sway, 0.54),
            (0.45 + sway, 0.60), (0.55 + sway, 0.60),
            (0.45, 0.68), (0.55, 0.68), (0.43, 0.80), (0.57, 0.80),
            (0.43, 0.92), (0.57, 0.92),
        ]
    else:
        coords = [
            (0.50 + sway, 0.16), (0.485 + sway, 0.14), (0.515 + sway, 0.14),
            (0.47 + sway, 0.16), (0.53 + sway, 0.16),
            (0.41 + sway, 0.30), (0.59 + sway, 0.30),
            (0.37 + sway, 0.45), (0.63 + sway, 0.45),
            (0.36 + sway, 0.58), (0.64 + sway, 0.58),
            (0.44, 0.56), (0.56, 0.56), (0.44, 0.75), (0.56, 0.75),
            (0.44, 0.92), (0.56, 0.92),
        ]
    return [
        SkeletonKeypoint(name=n, x=round(x, 4), y=round(y, 4), conf=0.91)
        for n, (x, y) in zip(COCO_17_NAMES, coords)
    ]


def _write_track(
    out_path: Path,
    track_id: str,
    title: str,
    scenario_id: ScenarioId,
    dataset_source: str,
    description: str,
    samples: list[ReplaySample],
) -> None:
    meta = {
        "_meta": True,
        "track_id": track_id,
        "title": title,
        "scenario_id": scenario_id.value,
        "dataset_source": dataset_source,
        "description": description,
        "fps_nominal": 8.0,
        "is_uploaded": False,
    }
    lines = [json.dumps(meta, ensure_ascii=False)]
    lines.extend(s.model_dump_json() for s in samples)
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_a1_track(rng: random.Random) -> list[ReplaySample]:
    """A1 軌跡：空場(24) -> 寵物/水蒸氣誤報陷阱(32) -> 浴室跌倒與微弱呼吸(40) -> 復歸(24)。"""
    samples: list[ReplaySample] = []
    for i in range(120):
        ts = 1000.0 + i * 125.0
        phase = i * 0.32
        if i < 24:
            seg, alert, single_fa, note = "空場衛浴待機 (L0)", False, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.06, 0.04, 0.04)
            mmw = MmWaveFrame(timestamp_ms=ts - 16.0, points=[], centroid_z_m=0.0, target_distance_m=3.5)
            grid = _make_thermal_grid(rng, 24.5, [])
            thm = ThermalFrame(timestamp_ms=ts - 24.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=24.5)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0)
        elif i < 56:
            seg, alert, single_fa = "寵物掃地機＋熱水蒸氣干擾段", False, True
            note = "單純熱像或單純無線運動將誤報浴室異常"
            csi = _make_csi(ts - 4.0, rng, phase, 0.56, 0.44, 0.50)
            mmw = MmWaveFrame(
                timestamp_ms=ts - 14.0,
                points=_make_points(rng, 0.5 * math.sin(phase), 2.3, 0.28, 0.45, 0.15, 0.04, count=8, spread_z=0.07, rcs=0.18),
                centroid_z_m=0.28, vz_mps=0.04, horizontal_speed_mps=0.68,
                breathing_bpm=0.0, heart_rate_bpm=0.0, breathing_confidence=0.12,
                micro_doppler_human_score=0.23, target_distance_m=2.3,
            )
            grid = _make_thermal_grid(rng, 29.5, [(0.5, 0.5, 12.8, 0.48)])
            thm = ThermalFrame(
                timestamp_ms=ts - 22.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid),
                ambient_temp_c=29.5, human_body_temp_ratio=0.15, temp_rise_rate_c_per_min=1.2,
            )
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0)
        elif i < 96:
            seg, alert, single_fa, note = "浴室急墜跌倒與呼吸微弱 (真陽性)", True, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.85, 0.78, 0.84)
            vz = -1.55 if i < 66 else -0.88
            mmw = MmWaveFrame(
                timestamp_ms=ts - 15.0,
                points=_make_points(rng, 0.15, 2.1, 0.24, 0.02, 0.02, vz, count=22, spread_z=0.10),
                centroid_z_m=0.24, vz_mps=vz, horizontal_speed_mps=0.08,
                breathing_bpm=6.4, heart_rate_bpm=106.0, breathing_confidence=0.25,
                micro_doppler_human_score=0.88, target_distance_m=2.1,
            )
            grid = _make_thermal_grid(rng, 24.5, [(0.52, 0.68, 12.4, 0.16)])
            thm = ThermalFrame(
                timestamp_ms=ts - 22.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid),
                ambient_temp_c=24.5, human_body_temp_ratio=0.58, hotspot_x=0.52, hotspot_y=0.68,
            )
            rgb = RgbFrameMetadata(
                timestamp_ms=ts - 5.0, person_detected=True, posture_label="fallen",
                fall_aspect_ratio=1.92, skeleton_17pts=_make_skeleton("fallen", phase),
            )
        else:
            seg, alert, single_fa, note = "救援復歸期 (正常站立)", False, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.34, 0.28, 0.32, ble_ok=True)
            mmw = MmWaveFrame(
                timestamp_ms=ts - 14.0,
                points=_make_points(rng, 0.0, 2.2, 1.25, 0.1, 0.05, 0.01, count=18),
                centroid_z_m=1.25, vz_mps=0.01, horizontal_speed_mps=0.35,
                breathing_bpm=16.0, heart_rate_bpm=78.0, breathing_confidence=0.85,
                micro_doppler_human_score=0.84, target_distance_m=2.2,
            )
            grid = _make_thermal_grid(rng, 24.5, [(0.5, 0.46, 12.0, 0.15)])
            thm = ThermalFrame(
                timestamp_ms=ts - 22.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid),
                ambient_temp_c=24.5, human_body_temp_ratio=0.52, hotspot_x=0.5, hotspot_y=0.46,
            )
            rgb = RgbFrameMetadata(
                timestamp_ms=ts - 6.0, person_detected=True, posture_label="standing",
                fall_aspect_ratio=0.42, skeleton_17pts=_make_skeleton("standing", phase),
            )
        samples.append(ReplaySample(
            frame_idx=i, timestamp_ms=ts, csi=csi, mmwave=mmw, thermal=thm, rgb_hint=rgb,
            gt_segment=seg, gt_is_alert=alert,
            single_modality_would_false_alarm=single_fa, single_modality_note=note,
        ))
    return samples


def build_c4_track(rng: random.Random) -> list[ReplaySample]:
    """C4 軌跡：平穩(24) -> EV 煞車餘熱與日照反光(32) -> Stage 1 熱失控+濃煙搜救(40) -> 降溫恢復(24)。"""
    samples: list[ReplaySample] = []
    for i in range(120):
        ts = 1000.0 + i * 125.0
        phase = i * 0.30
        if i < 24:
            seg, alert, single_fa, note = "BESS 儲能櫃恆溫待命", False, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.05, 0.04, 0.04)
            mmw = MmWaveFrame(timestamp_ms=ts - 14.0, points=[], centroid_z_m=0.0, target_distance_m=4.0)
            grid = _make_thermal_grid(rng, 26.5, [(0.5, 0.5, 4.0, 0.16)])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=26.5)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0)
        elif i < 56:
            seg, alert, single_fa = "EV 車輛煞車盤餘熱與陽光反光干擾", False, True
            note = "純熱像門檻 (>55°C) 將誤觸 BMS 急停"
            csi = _make_csi(ts - 4.0, rng, phase, 0.25, 0.20, 0.22)
            mmw = MmWaveFrame(timestamp_ms=ts - 14.0, points=[], centroid_z_m=0.0, target_distance_m=4.5)
            grid = _make_thermal_grid(rng, 28.0, [(0.75, 0.62, 34.0, 0.12)])
            thm = ThermalFrame(
                timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid),
                ambient_temp_c=28.0, temp_rise_rate_c_per_min=0.8, hotspot_x=0.75, hotspot_y=0.62,
                is_glass_boundary_reflection=True,
            )
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0, vehicle_or_glare_heat_mask=True)
        elif i < 96:
            seg, alert, single_fa, note = "電芯熱失控 Stage 1＋濃煙受困人員定位", True, False, ""
            temp_delta = 38.0 + (i - 56) * 0.55
            csi = _make_csi(ts - 3.0, rng, phase, 0.74, 0.66, 0.72)
            mmw = MmWaveFrame(
                timestamp_ms=ts - 15.0,
                points=_make_points(rng, -0.35, 3.0, 0.95, 0.08, 0.04, -0.02, count=20),
                centroid_z_m=0.95, vz_mps=-0.02, horizontal_speed_mps=0.14,
                breathing_bpm=25.5, heart_rate_bpm=116.0, breathing_confidence=0.68,
                micro_doppler_human_score=0.80, smoke_penetration_active=True, target_distance_m=3.0,
            )
            grid = _make_thermal_grid(rng, 30.5, [(0.68, 0.42, temp_delta, 0.11)])
            thm = ThermalFrame(
                timestamp_ms=ts - 22.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid),
                ambient_temp_c=30.5, human_body_temp_ratio=0.35, temp_rise_rate_c_per_min=11.2,
                hotspot_x=0.68, hotspot_y=0.42,
            )
            rgb = RgbFrameMetadata(
                timestamp_ms=ts - 6.0, person_detected=True, posture_label="standing",
                smoke_or_flame_visual=True, skeleton_17pts=_make_skeleton("standing", phase),
            )
        else:
            seg, alert, single_fa, note = "BMS 切斷後冷卻恢復期", False, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.08, 0.06, 0.06)
            mmw = MmWaveFrame(timestamp_ms=ts - 14.0, points=[], centroid_z_m=0.0, target_distance_m=4.0)
            grid = _make_thermal_grid(rng, 28.0, [(0.68, 0.42, 10.0, 0.14)])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=28.0, temp_rise_rate_c_per_min=-2.4)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0)
        samples.append(ReplaySample(
            frame_idx=i, timestamp_ms=ts, csi=csi, mmwave=mmw, thermal=thm, rgb_hint=rgb,
            gt_segment=seg, gt_is_alert=alert,
            single_modality_would_false_alarm=single_fa, single_modality_note=note,
        ))
    return samples


def build_b1_track(rng: random.Random) -> list[ReplaySample]:
    """B1 軌跡：夜間空場(24) -> 授權巡邏白名單與野狗/熱反光(32) -> 非授權雙足翻越入侵(40) -> 恢復(24)。"""
    samples: list[ReplaySample] = []
    for i in range(120):
        ts = 1000.0 + i * 125.0
        phase = i * 0.30
        if i < 24:
            seg, alert, single_fa, note = "夜間周界靜默巡檢", False, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.06, 0.04, 0.04)
            mmw = MmWaveFrame(timestamp_ms=ts - 14.0, points=[], centroid_z_m=0.0, target_distance_m=5.5)
            grid = _make_thermal_grid(rng, 22.0, [])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=22.0)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0)
        elif i < 40:
            seg, alert, single_fa = "BLE/UWB 白名單保全巡檢段", False, True
            note = "傳統無身份認證攝影機/PIR 將誤報保全人員入侵"
            csi = _make_csi(ts - 3.0, rng, phase, 0.50, 0.42, 0.48, ble_ok=True, uwb_auth=True)
            mmw = MmWaveFrame(
                timestamp_ms=ts - 14.0,
                points=_make_points(rng, 0.1, 3.8, 1.32, 0.1, -0.8, 0.0, count=20),
                centroid_z_m=1.32, horizontal_speed_mps=0.85,
                breathing_bpm=18.0, breathing_confidence=0.75,
                micro_doppler_human_score=0.86, target_distance_m=3.8,
            )
            grid = _make_thermal_grid(rng, 22.5, [(0.5, 0.5, 13.5, 0.14)])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=22.5, human_body_temp_ratio=0.52)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0, person_detected=True, posture_label="standing", skeleton_17pts=_make_skeleton("standing", phase))
        elif i < 56:
            seg, alert, single_fa = "野狗竄越＋強光熱反光干擾段", False, True
            note = "PIR / 視覺移動偵測受野狗與反光觸發每日 20–50 次誤報"
            csi = _make_csi(ts - 3.0, rng, phase, 0.42, 0.36, 0.40)
            mmw = MmWaveFrame(
                timestamp_ms=ts - 14.0,
                points=_make_points(rng, 0.8 * math.cos(phase), 4.0, 0.36, 0.8, 0.2, 0.0, count=9, spread_z=0.09, rcs=0.22),
                centroid_z_m=0.36, horizontal_speed_mps=1.1, micro_doppler_human_score=0.26, target_distance_m=4.0,
            )
            grid = _make_thermal_grid(rng, 28.0, [(0.78, 0.24, 21.0, 0.18)])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=28.0, human_body_temp_ratio=0.12, is_glass_boundary_reflection=True)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0, vehicle_or_glare_heat_mask=True)
        elif i < 96:
            seg, alert, single_fa, note = "非授權人員翻越圍籬入侵 (真陽性)", True, False, ""
            dist = max(1.4, 5.2 - (i - 56) * 0.09)
            csi = _make_csi(ts - 3.0, rng, phase, 0.82, 0.74, 0.82)
            mmw = MmWaveFrame(
                timestamp_ms=ts - 14.0,
                points=_make_points(rng, 0.25, dist, 1.32, 0.12, -1.25, 0.02, count=24),
                centroid_z_m=1.32, vz_mps=0.02, horizontal_speed_mps=1.32,
                breathing_bpm=21.0, heart_rate_bpm=98.0, breathing_confidence=0.72,
                micro_doppler_human_score=0.89, target_distance_m=dist,
            )
            grid = _make_thermal_grid(rng, 22.0, [(0.54, 0.50, 14.2, 0.14)])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=22.0, human_body_temp_ratio=0.56, hotspot_x=0.54, hotspot_y=0.50)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0, person_detected=True, posture_label="standing", skeleton_17pts=_make_skeleton("standing", phase))
        else:
            seg, alert, single_fa, note = "入侵目標離開恢復平靜", False, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.06, 0.04, 0.04)
            mmw = MmWaveFrame(timestamp_ms=ts - 14.0, points=[], centroid_z_m=0.0, target_distance_m=5.5)
            grid = _make_thermal_grid(rng, 22.0, [])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=22.0)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0)
        samples.append(ReplaySample(
            frame_idx=i, timestamp_ms=ts, csi=csi, mmwave=mmw, thermal=thm, rgb_hint=rgb,
            gt_segment=seg, gt_is_alert=alert,
            single_modality_would_false_alarm=single_fa, single_modality_note=note,
        ))
    return samples


def build_c1_track(rng: random.Random) -> list[ReplaySample]:
    """C1 軌跡：產線運作(24) -> 清洗水霧與晃動電纜(32) -> UWB 跨貨架盲區防撞(40) -> 復歸(24)。"""
    samples: list[ReplaySample] = []
    for i in range(120):
        ts = 1000.0 + i * 125.0
        phase = i * 0.34
        if i < 24:
            seg, alert, single_fa, note = "HRC 安全全速作業區", False, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.14, 0.10, 0.12, uwb_dist=4.2, uwb_auth=True)
            mmw = MmWaveFrame(timestamp_ms=ts - 14.0, points=_make_points(rng, 0.2, 3.8, 1.2, 0.05, 0.05, 0.0, count=12), centroid_z_m=1.2, horizontal_speed_mps=0.2, breathing_confidence=0.7, micro_doppler_human_score=0.72, target_distance_m=3.8)
            grid = _make_thermal_grid(rng, 23.0, [(0.48, 0.48, 12.5, 0.14)])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=23.0, human_body_temp_ratio=0.45)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0, person_detected=True, posture_label="standing", ppe_compliant=True)
        elif i < 56:
            seg, alert, single_fa = "高壓清洗水霧＋垂掛電纜晃動干擾", False, True
            note = "傳統安全光柵／純毫米波將誤急停，導致產線高額停機損失"
            csi = _make_csi(ts - 3.0, rng, phase, 0.50, 0.46, 0.45, uwb_dist=6.2, uwb_auth=True, uwb_nlos=False)
            mmw = MmWaveFrame(
                timestamp_ms=ts - 14.0,
                points=_make_points(rng, 0.2 * math.sin(phase * 2.0), 1.4, 1.45, 0.35, 0.05, 0.02, count=14, spread_xy=0.32, rcs=0.15),
                centroid_z_m=1.45, horizontal_speed_mps=0.35, micro_doppler_human_score=0.24, target_distance_m=1.4,
            )
            grid = _make_thermal_grid(rng, 22.5, [(0.50, 0.40, 2.2, 0.25)])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=22.5, human_body_temp_ratio=0.05)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0)
        elif i < 96:
            seg, alert, single_fa, note = "作業員 UWB 盲區逼近機械臂 (<0.9m 真陽性)", True, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.86, 0.80, 0.88, uwb_dist=0.82, uwb_auth=True, uwb_nlos=True)
            mmw = MmWaveFrame(
                timestamp_ms=ts - 14.0,
                points=_make_points(rng, 0.1, 0.85, 1.28, 0.05, -1.10, 0.0, count=24),
                centroid_z_m=1.28, horizontal_speed_mps=1.15,
                breathing_bpm=19.0, heart_rate_bpm=92.0, breathing_confidence=0.76,
                micro_doppler_human_score=0.86, target_distance_m=0.85,
            )
            grid = _make_thermal_grid(rng, 23.0, [(0.48, 0.48, 12.8, 0.15)])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=23.0, human_body_temp_ratio=0.54)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0, person_detected=True, posture_label="standing", ppe_compliant=True, skeleton_17pts=_make_skeleton("standing", phase))
        else:
            seg, alert, single_fa, note = "作業員退離危險區恢復全速", False, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.15, 0.12, 0.12, uwb_dist=4.5, uwb_auth=True)
            mmw = MmWaveFrame(timestamp_ms=ts - 14.0, points=_make_points(rng, 0.2, 4.2, 1.25, 0.02, 0.02, 0.0, count=12), centroid_z_m=1.25, horizontal_speed_mps=0.1, breathing_confidence=0.75, micro_doppler_human_score=0.75, target_distance_m=4.2)
            grid = _make_thermal_grid(rng, 23.0, [(0.48, 0.48, 12.0, 0.14)])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=23.0, human_body_temp_ratio=0.42)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0, person_detected=True, posture_label="standing", ppe_compliant=True)
        samples.append(ReplaySample(
            frame_idx=i, timestamp_ms=ts, csi=csi, mmwave=mmw, thermal=thm, rgb_hint=rgb,
            gt_segment=seg, gt_is_alert=alert,
            single_modality_would_false_alarm=single_fa, single_modality_note=note,
        ))
    return samples


def build_b2_track(rng: random.Random) -> list[ReplaySample]:
    """B2 軌跡：空場節能(24) -> 玻璃外走廊行人+靜態電器(36) -> 靜坐呼吸零False-Off(40) -> 離席節能(20)。"""
    samples: list[ReplaySample] = []
    for i in range(120):
        ts = 1000.0 + i * 125.0
        phase = i * 0.28
        if i < 24:
            seg, alert, occ, single_fa, note = "會議室無人節能待機 (L0)", False, False, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.05, 0.04, 0.04)
            mmw = MmWaveFrame(timestamp_ms=ts - 14.0, points=[], centroid_z_m=0.0, target_distance_m=4.5)
            grid = _make_thermal_grid(rng, 23.5, [])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=23.5)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0)
        elif i < 60:
            seg, alert, occ, single_fa = "玻璃隔間外走廊路人＋咖啡機熱源干擾", False, False, True
            note = "純毫米波穿牆或紅外線熱感將誤開冷氣照明"
            csi = _make_csi(ts - 3.0, rng, phase, 0.34, 0.28, 0.32)
            mmw = MmWaveFrame(
                timestamp_ms=ts - 14.0,
                points=_make_points(rng, 0.8 * math.sin(phase * 0.6), 4.6, 1.30, 0.70, 0.0, 0.0, count=14),
                centroid_z_m=1.30, horizontal_speed_mps=0.92,
                breathing_bpm=0.0, breathing_confidence=0.16,
                micro_doppler_human_score=0.46, target_distance_m=4.6,
            )
            grid = _make_thermal_grid(rng, 23.5, [(0.86, 0.50, 15.0, 0.12)])
            thm = ThermalFrame(
                timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid),
                ambient_temp_c=23.5, human_body_temp_ratio=0.12, hotspot_x=0.86, hotspot_y=0.50,
                is_static_heat_source=True, is_glass_boundary_reflection=True,
            )
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0)
        elif i < 100:
            seg, alert, occ, single_fa = "員工靜坐辦公呼吸存在確認 (零 False-Off / L1 禁開鏡)", False, True, True
            note = "傳統 PIR 因靜坐不動常於 15 分鐘誤關冷氣 (False-Off)"
            csi = _make_csi(ts - 3.0, rng, phase, 0.24, 0.16, 0.20, ble_ok=True)
            mmw = MmWaveFrame(
                timestamp_ms=ts - 14.0,
                points=_make_points(rng, 0.05, 2.4, 0.98, 0.01, 0.01, 0.0, count=16, spread_xy=0.20),
                centroid_z_m=0.98, horizontal_speed_mps=0.04,
                breathing_bpm=15.4 + 0.4 * math.sin(phase), heart_rate_bpm=69.0,
                breathing_confidence=0.91, micro_doppler_human_score=0.78, target_distance_m=2.4,
            )
            grid = _make_thermal_grid(rng, 24.0, [(0.50, 0.50, 12.1, 0.16)])
            thm = ThermalFrame(
                timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid),
                ambient_temp_c=24.0, human_body_temp_ratio=0.50, hotspot_x=0.50, hotspot_y=0.50,
            )
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0, person_detected=True, posture_label="sitting", skeleton_17pts=_make_skeleton("sitting", phase))
        else:
            seg, alert, occ, single_fa, note = "員工離席切回節能退縮", False, False, False, ""
            csi = _make_csi(ts - 3.0, rng, phase, 0.06, 0.04, 0.04)
            mmw = MmWaveFrame(timestamp_ms=ts - 14.0, points=[], centroid_z_m=0.0, target_distance_m=4.5)
            grid = _make_thermal_grid(rng, 23.5, [])
            thm = ThermalFrame(timestamp_ms=ts - 20.0, grid_flat=grid, max_temp_c=max(grid), min_temp_c=min(grid), ambient_temp_c=23.5)
            rgb = RgbFrameMetadata(timestamp_ms=ts - 6.0)
        samples.append(ReplaySample(
            frame_idx=i, timestamp_ms=ts, csi=csi, mmwave=mmw, thermal=thm, rgb_hint=rgb,
            gt_segment=seg, gt_is_alert=alert, gt_occupied=occ,
            single_modality_would_false_alarm=single_fa, single_modality_note=note,
        ))
    return samples


def main() -> None:
    out_dir = PROJECT_ROOT / "datasets"
    out_dir.mkdir(parents=True, exist_ok=True)
    rng = random.Random(20261122)

    specs = [
        (
            "a1_elderly_fall_and_pet_replay",
            "A1 長照衛浴跌倒與寵物/水蒸氣干擾對照軌跡",
            ScenarioId.A1,
            "公開基準仿照：TI 60GHz 跌倒/呼吸點雲 + ESP-CSI 室內部署 + MLX90640 衛浴水蒸氣序列",
            "包含空場、掃地機寵物/熱水蒸氣誤報陷阱段、急墜跌倒異常段與恢復期（共 120 幀 @ 8Hz）",
            build_a1_track(rng),
        ),
        (
            "c4_bess_runaway_smoke_replay",
            "C4 BESS 儲能熱失控 Stage 1 與濃煙搜救軌跡",
            ScenarioId.C4,
            "公開基準仿照：FLIR/VOx 電池熱失控溫升序列 + 濃煙霧室 mmWave 人體穿透點雲",
            "包含平穩待命、EV 車輛煞車餘熱反光陷阱、電芯升溫破 80°C+濃煙受困與降溫期（共 120 幀 @ 8Hz）",
            build_c4_track(rng),
        ),
        (
            "b1_perimeter_mixed_replay",
            "B1 園區周界白名單、動物眩光與翻越入侵軌跡",
            ScenarioId.B1,
            "公開基準仿照：BLE/UWB 巡邏信標 + 戶外 60/77GHz 步態點雲 + LWIR 熱斑序列",
            "包含空場、BLE/UWB 授權保全、野狗與熱反光陷阱、翻越入侵與離開恢復（共 120 幀 @ 8Hz）",
            build_b1_track(rng),
        ),
        (
            "c1_hrc_factory_replay",
            "C1 智慧工廠 HRC 水霧電纜與跨貨架防撞軌跡",
            ScenarioId.C1,
            "公開基準仿照：UWB 802.15.4z NLOS 測距 + 60GHz 工業水霧散射與人體逼近速度點雲",
            "包含正常協作、清洗水霧與電纜晃動誤急停陷阱、UWB 盲區近距碰撞預警與退離（共 120 幀 @ 8Hz）",
            build_c1_track(rng),
        ),
        (
            "b2_office_hvac_replay",
            "B2 智慧商辦玻璃外路人排除與靜坐零 False-Off 軌跡",
            ScenarioId.B2,
            "公開基準仿照：天花板 802.11bf AP + 60GHz 胸腔微動呼吸 + 32×24 熱像隔間測試",
            "包含空場節能、玻璃隔間外路人＋靜態熱源、靜坐辦公零誤關（強制 L1 禁開鏡）與離席（共 120 幀 @ 8Hz）",
            build_b2_track(rng),
        ),
    ]

    for tid, title, sid, src, desc, samples in specs:
        out_fp = out_dir / f"{tid}.jsonl"
        _write_track(out_fp, tid, title, sid, src, desc, samples)
        print(f"已產出: {out_fp} ({len(samples)} 幀)")


if __name__ == "__main__":
    main()
