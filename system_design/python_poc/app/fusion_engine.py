"""多模態時空對齊與 Dempster-Shafer (D-S) 證據融合核心引擎。

包含兩大核心元件：
1. `SlidingWindowAligner`：實作 100 ms 滑動視窗對齊器，支援 100–1000 Hz Wi-Fi CSI
   降維聚合，並透過簡易 3D 等速卡爾曼濾波器（Kalman Filter）執行 mmWave 速度外推與
   Thermal 雙幀線性插值，消除異質幀率造成的空間錯位。
2. `DempsterShaferFusionEngine`：實作正統 Dempster-Shafer 證據理論（Frame of
   Discernment Θ = {Normal, Alert}），計算各模態 BPA、全域衝突係數 K、
   Belief / Plausibility 區間，並具備 K >= 0.99 防除零安全回退與高衝突仲裁標記。
"""

from __future__ import annotations

from collections import deque
from typing import Optional

import numpy as np

from .models import (
    AlignedFrame,
    CsiFrame,
    DsFusionResult,
    MmWaveFrame,
    ModalityBpa,
    PrivacyLevel,
    RgbFrameMetadata,
    ScenarioId,
    ThermalFrame,
)


class SimpleKalman3D:
    """輕量級 6 狀態（位置 x,y,z + 速度 vx,vy,vz）等速卡爾曼濾波器。"""

    def __init__(self) -> None:
        self._initialized: bool = False
        self._last_ts_ms: float = 0.0
        # 狀態向量 [x, y, z, vx, vy, vz]^T
        self.state = np.zeros(6, dtype=np.float64)
        self.cov = np.eye(6, dtype=np.float64) * 1.0
        self.H = np.eye(6, dtype=np.float64)
        # 量測雜訊共變異數（位置精度較高，速度雜訊適中）
        self.R = np.diag([0.04, 0.04, 0.03, 0.12, 0.12, 0.10])

    def reset(self) -> None:
        """重設卡爾曼濾波器狀態。"""
        self._initialized = False
        self._last_ts_ms = 0.0
        self.state.fill(0.0)
        self.cov = np.eye(6, dtype=np.float64)

    def update(self, frame: MmWaveFrame) -> tuple[float, float, float, float, float, float]:
        """以最新 mmWave 點雲質心與速度更新濾波器狀態。"""
        if frame.points:
            px = float(np.mean([p.x for p in frame.points]))
            py = float(np.mean([p.y for p in frame.points]))
            pz = float(np.mean([p.z for p in frame.points]))
            vx = float(np.mean([p.vx for p in frame.points]))
            vy = float(np.mean([p.vy for p in frame.points]))
            vz = float(np.mean([p.vz for p in frame.points]))
        else:
            px = 0.0
            py = frame.target_distance_m
            pz = frame.centroid_z_m
            vx = 0.0
            vy = frame.horizontal_speed_mps
            vz = frame.vz_mps

        z_meas = np.array([px, py, pz, vx, vy, vz], dtype=np.float64)

        if not self._initialized or frame.timestamp_ms < self._last_ts_ms:
            self.state = z_meas.copy()
            self.cov = np.eye(6, dtype=np.float64) * 0.25
            self._last_ts_ms = frame.timestamp_ms
            self._initialized = True
            return tuple(float(v) for v in self.state)  # type: ignore[return-value]

        dt_s = max(0.0, min(0.5, (frame.timestamp_ms - self._last_ts_ms) / 1000.0))
        self._last_ts_ms = frame.timestamp_ms

        F = np.eye(6, dtype=np.float64)
        F[0, 3] = dt_s
        F[1, 4] = dt_s
        F[2, 5] = dt_s

        q_pos = 0.02 * (dt_s + 0.01)
        q_vel = 0.15 * (dt_s + 0.01)
        Q = np.diag([q_pos, q_pos, q_pos, q_vel, q_vel, q_vel])

        # Predict
        x_pred = F @ self.state
        P_pred = F @ self.cov @ F.T + Q

        # Update
        y_res = z_meas - (self.H @ x_pred)
        S = self.H @ P_pred @ self.H.T + self.R
        K_gain = P_pred @ self.H.T @ np.linalg.inv(S)

        self.state = x_pred + K_gain @ y_res
        self.cov = (np.eye(6, dtype=np.float64) - K_gain @ self.H) @ P_pred

        return tuple(float(v) for v in self.state)  # type: ignore[return-value]

    def predict_at(self, target_ts_ms: float) -> tuple[float, float, float]:
        """依據目前速度向量線性外推目標於 `target_ts_ms` 時刻之 3D 座標。"""
        if not self._initialized:
            return (0.0, 0.0, 0.0)
        dt_s = (target_ts_ms - self._last_ts_ms) / 1000.0
        # 限制外推跨度避免過舊影格無限發散
        dt_s = max(-0.15, min(0.15, dt_s))
        px = float(self.state[0] + self.state[3] * dt_s)
        py = float(self.state[1] + self.state[4] * dt_s)
        pz = float(max(0.0, self.state[2] + self.state[5] * dt_s))
        return (px, py, pz)


class SlidingWindowAligner:
    """100 ms 滑動時間視窗對齊器。

    將不同採樣率（100–1000 Hz CSI、10–20 Hz mmWave、9–30 Hz Thermal、30 fps RGB）
    的非同步感測幀對齊至指定目標時間點 `target_timestamp_ms`。超過視窗半寬/全寬的過時
    資料將自動忽略，不納入當次融合計算。
    """

    def __init__(self, window_ms: float = 100.0, tolerance_ms: float = 15.0) -> None:
        self.window_ms = window_ms
        self.tolerance_ms = tolerance_ms
        self._csi_buf: deque[CsiFrame] = deque(maxlen=512)
        self._mmwave_buf: deque[MmWaveFrame] = deque(maxlen=128)
        self._thermal_buf: deque[ThermalFrame] = deque(maxlen=128)
        self._rgb_buf: deque[RgbFrameMetadata] = deque(maxlen=128)
        self._kalman = SimpleKalman3D()

    def clear(self) -> None:
        """清除所有感測緩衝與卡爾曼狀態。"""
        self._csi_buf.clear()
        self._mmwave_buf.clear()
        self._thermal_buf.clear()
        self._rgb_buf.clear()
        self._kalman.reset()

    def push_csi(self, frame: CsiFrame) -> None:
        """寫入最新 Wi-Fi CSI 幀。"""
        self._csi_buf.append(frame)

    def push_mmwave(self, frame: MmWaveFrame) -> None:
        """寫入最新 mmWave 幀並更新卡爾曼速度濾波器。"""
        self._mmwave_buf.append(frame)
        self._kalman.update(frame)

    def push_thermal(self, frame: ThermalFrame) -> None:
        """寫入最新 Thermal 熱陣列幀。"""
        self._thermal_buf.append(frame)

    def push_rgb(self, frame: RgbFrameMetadata) -> None:
        """寫入最新 RGB 去識別化推論元數據幀。"""
        self._rgb_buf.append(frame)

    def _in_window(self, ts_ms: float, target_ms: float) -> bool:
        """判斷時間戳是否落於滑動對齊視窗內（|ts - target| <= window_ms）。"""
        return abs(ts_ms - target_ms) <= self.window_ms

    def _pick_closest(self, buf: deque, target_ms: float):
        candidates = [f for f in buf if self._in_window(f.timestamp_ms, target_ms)]
        if not candidates:
            return None, 0.0
        best = min(candidates, key=lambda f: abs(f.timestamp_ms - target_ms))
        return best, target_ms - best.timestamp_ms

    def align(self, target_timestamp_ms: float) -> AlignedFrame:
        """對齊所有落於 `[target - window_ms, target + window_ms]` 內的多模態幀。"""
        # 1. Wi-Fi CSI 高頻降維聚合（取視窗內多筆 CSI 平均值以抑制瞬時雜訊）
        csi_candidates = [f for f in self._csi_buf if self._in_window(f.timestamp_ms, target_timestamp_ms)]
        aligned_csi: Optional[CsiFrame] = None
        csi_offset = 0.0
        if csi_candidates:
            latest_csi = min(csi_candidates, key=lambda f: abs(f.timestamp_ms - target_timestamp_ms))
            csi_offset = target_timestamp_ms - latest_csi.timestamp_ms
            # 優先取 [target-25ms, target+25ms] 子窗做平滑，無則取全視窗候選
            tight = [f for f in csi_candidates if abs(f.timestamp_ms - target_timestamp_ms) <= 25.0]
            pool = tight if tight else csi_candidates
            aligned_csi = latest_csi.model_copy(
                update={
                    "fresnel_variance": float(np.mean([f.fresnel_variance for f in pool])),
                    "doppler_energy": float(np.mean([f.doppler_energy for f in pool])),
                    "motion_score": float(np.mean([f.motion_score for f in pool])),
                }
            )

        # 2. mmWave 選擇視窗內最近幀，並以卡爾曼速度外推補償時間差
        mmw_raw, mmw_offset = self._pick_closest(self._mmwave_buf, target_timestamp_ms)
        aligned_mmw: Optional[MmWaveFrame] = None
        mmw_extrapolated = False
        extrapolated_pos = (0.0, 0.0, 0.0)
        if mmw_raw is not None:
            dt_s = mmw_offset / 1000.0
            if abs(mmw_offset) > 0.5:
                mmw_extrapolated = True
                ex, ey, ez = self._kalman.predict_at(target_timestamp_ms)
                extrapolated_pos = (round(ex, 4), round(ey, 4), round(ez, 4))
                comp_z = max(0.0, min(3.0, mmw_raw.centroid_z_m + mmw_raw.vz_mps * dt_s))
                comp_dist = max(0.1, mmw_raw.target_distance_m + mmw_raw.horizontal_speed_mps * dt_s * 0.2)
                aligned_mmw = mmw_raw.model_copy(
                    update={
                        "centroid_z_m": round(comp_z, 4),
                        "target_distance_m": round(comp_dist, 4),
                    }
                )
            else:
                if mmw_raw.points:
                    extrapolated_pos = (
                        round(float(np.mean([p.x for p in mmw_raw.points])), 4),
                        round(float(np.mean([p.y for p in mmw_raw.points])), 4),
                        round(float(np.mean([p.z for p in mmw_raw.points])), 4),
                    )
                else:
                    extrapolated_pos = (0.0, mmw_raw.target_distance_m, mmw_raw.centroid_z_m)
                aligned_mmw = mmw_raw

        # 3. Thermal 雙幀線性插值或單幀斜率補償
        thm_candidates = [f for f in self._thermal_buf if self._in_window(f.timestamp_ms, target_timestamp_ms)]
        aligned_thm: Optional[ThermalFrame] = None
        thm_offset = 0.0
        if thm_candidates:
            before = [f for f in thm_candidates if f.timestamp_ms <= target_timestamp_ms]
            after = [f for f in thm_candidates if f.timestamp_ms >= target_timestamp_ms]
            if before and after:
                f0 = max(before, key=lambda f: f.timestamp_ms)
                f1 = min(after, key=lambda f: f.timestamp_ms)
                if f1.timestamp_ms > f0.timestamp_ms:
                    alpha = (target_timestamp_ms - f0.timestamp_ms) / (f1.timestamp_ms - f0.timestamp_ms)
                    interp_max = f0.max_temp_c + alpha * (f1.max_temp_c - f0.max_temp_c)
                    interp_hx = f0.hotspot_x + alpha * (f1.hotspot_x - f0.hotspot_x)
                    interp_hy = f0.hotspot_y + alpha * (f1.hotspot_y - f0.hotspot_y)
                    thm_offset = min(
                        abs(target_timestamp_ms - f0.timestamp_ms),
                        abs(f1.timestamp_ms - target_timestamp_ms),
                    )
                    aligned_thm = f1.model_copy(
                        update={
                            "max_temp_c": round(interp_max, 3),
                            "hotspot_x": round(interp_hx, 4),
                            "hotspot_y": round(interp_hy, 4),
                        }
                    )
                else:
                    aligned_thm = f0
                    thm_offset = target_timestamp_ms - f0.timestamp_ms
            else:
                best_thm = min(thm_candidates, key=lambda f: abs(f.timestamp_ms - target_timestamp_ms))
                thm_offset = target_timestamp_ms - best_thm.timestamp_ms
                dt_min = thm_offset / 60000.0
                extrap_temp = best_thm.max_temp_c + best_thm.temp_rise_rate_c_per_min * dt_min
                aligned_thm = best_thm.model_copy(update={"max_temp_c": round(extrap_temp, 3)})

        # 4. RGB 元數據對齊（若 L0/L1 關閉則為 None）
        aligned_rgb, rgb_offset = self._pick_closest(self._rgb_buf, target_timestamp_ms)

        # 5. 計算對齊品質分數（以 15ms 容許偏差與實際有效模態數評估）
        offsets = []
        if aligned_csi is not None:
            offsets.append(abs(csi_offset))
        if aligned_mmw is not None:
            offsets.append(abs(mmw_offset))
        if aligned_thm is not None:
            offsets.append(abs(thm_offset))
        if aligned_rgb is not None:
            offsets.append(abs(rgb_offset))

        if not offsets:
            quality = 0.0
        else:
            avg_offset = float(np.mean(offsets))
            # <= tolerance_ms 視為滿分接近 1.0，線性衰減至 window_ms
            penalty = max(0.0, (avg_offset - self.tolerance_ms) / max(1.0, self.window_ms - self.tolerance_ms))
            quality = round(max(0.1, min(1.0, 1.0 - 0.65 * penalty)), 4)

        return AlignedFrame(
            target_timestamp_ms=target_timestamp_ms,
            window_ms=self.window_ms,
            csi=aligned_csi,
            mmwave=aligned_mmw,
            thermal=aligned_thm,
            rgb=aligned_rgb,
            csi_offset_ms=round(csi_offset, 3),
            mmwave_offset_ms=round(mmw_offset, 3),
            thermal_offset_ms=round(thm_offset, 3),
            rgb_offset_ms=round(rgb_offset, 3),
            mmwave_extrapolated=mmw_extrapolated,
            extrapolated_position=extrapolated_pos,
            alignment_quality=quality,
        )


def _clamp_bpa(m_normal: float, m_alert: float, m_uncertainty: float) -> tuple[float, float, float]:
    """確保 BPA 三元組非負且總和精確歸一化為 1.0。"""
    mn = max(0.0, m_normal)
    ma = max(0.0, m_alert)
    mu = max(0.0, m_uncertainty)
    total = mn + ma + mu
    if total <= 1e-12:
        return (0.0, 0.0, 1.0)
    return (mn / total, ma / total, mu / total)


class DempsterShaferFusionEngine:
    """Dempster-Shafer (D-S) 多模態證據融合引擎。

    辯識框架 Θ = {Normal, Alert}，非空焦點元素為 {Normal}, {Alert}, Θ (不確定度)。
    """

    HIGH_CONFLICT_THRESHOLD: float = 0.99

    @classmethod
    def combine_bpas(cls, bpas: list[ModalityBpa]) -> DsFusionResult:
        """對所有啟用中的模態 BPA 執行 Dempster 正交合併法則。

        正交合併公式：
          w(N) = w_old(N)*m(N) + w_old(N)*m(Θ) + w_old(Θ)*m(N)
          w(A) = w_old(A)*m(A) + w_old(A)*m(Θ) + w_old(Θ)*m(A)
          w(Θ) = w_old(Θ)*m(Θ)
          K = 1 - (w(N) + w(A) + w(Θ))
        當 K >= 0.99 時，啟動 Yager 安全防除零回退並設定 high_conflict_flag=True。
        """
        active_bpas = [b for b in bpas if b.active]
        if not active_bpas:
            return DsFusionResult(
                m_normal=0.0,
                m_alert=0.0,
                m_uncertainty=1.0,
                conflict_k=0.0,
                belief_alert=0.0,
                plausibility_alert=1.0,
                belief_normal=0.0,
                plausibility_normal=1.0,
                high_conflict_flag=False,
                modality_bpas=bpas,
            )

        # 未歸一化聯合質量初始為 Vacuous BPA: m(Θ) = 1.0
        w_n = 0.0
        w_a = 0.0
        w_u = 1.0

        for bpa in active_bpas:
            mn, ma, mu = _clamp_bpa(bpa.m_normal, bpa.m_alert, bpa.m_uncertainty)
            next_n = w_n * mn + w_n * mu + w_u * mn
            next_a = w_a * ma + w_a * mu + w_u * ma
            next_u = w_u * mu
            w_n, w_a, w_u = next_n, next_a, next_u

        norm_factor = w_n + w_a + w_u
        conflict_k = max(0.0, min(1.0, 1.0 - norm_factor))
        high_conflict = conflict_k >= cls.HIGH_CONFLICT_THRESHOLD or norm_factor <= 0.01

        if high_conflict:
            # Yager 安全回退：將高度矛盾的衝突質量轉入全集不確定度 Θ，防止除零並強制升階仲裁
            m_n = max(0.0, w_n)
            m_a = max(0.0, w_a)
            m_u = max(0.0, w_u + conflict_k)
            m_n, m_a, m_u = _clamp_bpa(m_n, m_a, m_u)
        else:
            m_n = w_n / norm_factor
            m_a = w_a / norm_factor
            m_u = w_u / norm_factor

        bel_alert = round(m_a, 6)
        pl_alert = round(min(1.0, m_a + m_u), 6)
        bel_normal = round(m_n, 6)
        pl_normal = round(min(1.0, m_n + m_u), 6)

        return DsFusionResult(
            m_normal=round(m_n, 6),
            m_alert=round(m_a, 6),
            m_uncertainty=round(m_u, 6),
            conflict_k=round(conflict_k, 6),
            belief_alert=bel_alert,
            plausibility_alert=pl_alert,
            belief_normal=bel_normal,
            plausibility_normal=pl_normal,
            high_conflict_flag=high_conflict,
            modality_bpas=bpas,
        )

    def compute_modality_bpas(
        self,
        aligned: AlignedFrame,
        scenario_id: ScenarioId,
        privacy_level: PrivacyLevel,
    ) -> list[ModalityBpa]:
        """依據對齊後的物理感測特徵與當前場景語意，計算四模態 BPA 質量分配。"""
        bpas: list[ModalityBpa] = []

        # 1. Wi-Fi CSI / BLE / UWB BPA
        if aligned.csi is not None:
            csi = aligned.csi
            if scenario_id == ScenarioId.C1 and csi.uwb_nlos_alert:
                bpa_csi = ModalityBpa(
                    modality="Wi-Fi CSI / UWB",
                    active=True,
                    m_normal=0.08,
                    m_alert=0.72,
                    m_uncertainty=0.20,
                    rationale=f"UWB NLOS 盲區標籤距離僅 {csi.uwb_tag_distance_m or 1.2:.1f}m",
                )
            elif (csi.ble_whitelist_present or csi.uwb_authorized) and scenario_id == ScenarioId.B1:
                bpa_csi = ModalityBpa(
                    modality="Wi-Fi CSI / BLE-UWB",
                    active=True,
                    m_normal=0.82,
                    m_alert=0.04,
                    m_uncertainty=0.14,
                    rationale="偵測到授權 BLE/UWB 巡邏白名單信標",
                )
            elif csi.fresnel_variance > 0.65 and not csi.ble_whitelist_present:
                bpa_csi = ModalityBpa(
                    modality="Wi-Fi CSI / BLE-UWB",
                    active=True,
                    m_normal=0.15,
                    m_alert=0.55,
                    m_uncertainty=0.30,
                    rationale=f"Fresnel 區劇烈擾動 (var={csi.fresnel_variance:.2f}) 且無白名單",
                )
            elif csi.fresnel_variance > 0.20:
                bpa_csi = ModalityBpa(
                    modality="Wi-Fi CSI / BLE-UWB",
                    active=True,
                    m_normal=0.50,
                    m_alert=0.22,
                    m_uncertainty=0.28,
                    rationale=f"一般人員活動擾動 (var={csi.fresnel_variance:.2f})",
                )
            else:
                bpa_csi = ModalityBpa(
                    modality="Wi-Fi CSI / BLE-UWB",
                    active=True,
                    m_normal=0.86,
                    m_alert=0.03,
                    m_uncertainty=0.11,
                    rationale="通道響應平穩（無人或靜態背景）",
                )
            bpas.append(bpa_csi)

        # 2. mmWave 60/77 GHz Radar BPA
        mmw_active = aligned.mmwave is not None and privacy_level != PrivacyLevel.L0
        if aligned.mmwave is not None:
            mmw = aligned.mmwave
            if not mmw_active:
                bpas.append(
                    ModalityBpa(
                        modality="mmWave Radar",
                        active=False,
                        m_normal=0.0,
                        m_alert=0.0,
                        m_uncertainty=1.0,
                        rationale="L0 LPDS 深度休眠中（0.5 mW）",
                    )
                )
            else:
                if scenario_id == ScenarioId.A1:
                    if mmw.vz_mps <= -0.8 or (mmw.centroid_z_m <= 0.55 and mmw.micro_doppler_human_score >= 0.5):
                        alert_mass = 0.78 if (mmw.breathing_bpm < 8.0 or mmw.breathing_confidence < 0.35) else 0.68
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.07,
                                m_alert=alert_mass,
                                m_uncertainty=round(0.93 - alert_mass, 4),
                                rationale=f"偵測急墜/低質心 (Z={mmw.centroid_z_m:.2f}m, Vz={mmw.vz_mps:.2f}m/s)",
                            )
                        )
                    elif mmw.micro_doppler_human_score < 0.40:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.72,
                                m_alert=0.08,
                                m_uncertainty=0.20,
                                rationale=f"非人類微多普勒特徵（疑似寵物/環境干擾, score={mmw.micro_doppler_human_score:.2f}）",
                            )
                        )
                    else:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.76,
                                m_alert=0.08,
                                m_uncertainty=0.16,
                                rationale=f"正常站姿/步態與呼吸穩定 ({mmw.breathing_bpm:.1f} bpm)",
                            )
                        )
                elif scenario_id == ScenarioId.C4:
                    if mmw.smoke_penetration_active and len(mmw.points) > 0:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.08,
                                m_alert=0.74,
                                m_uncertainty=0.18,
                                rationale="濃煙穿透模式鎖定現場滯留人員 3D 座標",
                            )
                        )
                    else:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.68,
                                m_alert=0.10,
                                m_uncertainty=0.22,
                                rationale="儲能櫃周邊無人員受困異常",
                            )
                        )
                elif scenario_id == ScenarioId.B1:
                    if mmw.micro_doppler_human_score >= 0.65 and mmw.horizontal_speed_mps >= 0.4 and mmw.centroid_z_m >= 0.8:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.08,
                                m_alert=0.76,
                                m_uncertainty=0.16,
                                rationale=f"確認人類雙足步態逼近 (v={mmw.horizontal_speed_mps:.2f}m/s)",
                            )
                        )
                    else:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.75,
                                m_alert=0.07,
                                m_uncertainty=0.18,
                                rationale="低矮目標或零淨位移振盪（樹枝/動物排除）",
                            )
                        )
                elif scenario_id == ScenarioId.C1:
                    if mmw.target_distance_m <= 1.8 and mmw.horizontal_speed_mps >= 0.5 and mmw.micro_doppler_human_score >= 0.5:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.06,
                                m_alert=0.78,
                                m_uncertainty=0.16,
                                rationale=f"近距高速逼近危險半徑 ({mmw.target_distance_m:.2f}m)",
                            )
                        )
                    elif len(mmw.points) > 0 and mmw.micro_doppler_human_score < 0.45:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.38,
                                m_alert=0.27,
                                m_uncertainty=0.35,
                                rationale="空間散射體疑似非人晃動（電纜/水霧待熱像複驗）",
                            )
                        )
                    else:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.80,
                                m_alert=0.05,
                                m_uncertainty=0.15,
                                rationale="安全協作距離內穩定追蹤",
                            )
                        )
                else:  # B2 Smart HVAC
                    if mmw.breathing_confidence >= 0.65 and 8.0 <= mmw.breathing_bpm <= 26.0:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.74,
                                m_alert=0.08,
                                m_uncertainty=0.18,
                                rationale=f"偵測靜坐人員正常呼吸微動 ({mmw.breathing_bpm:.1f} bpm)",
                            )
                        )
                    else:
                        bpas.append(
                            ModalityBpa(
                                modality="mmWave Radar",
                                active=True,
                                m_normal=0.65,
                                m_alert=0.12,
                                m_uncertainty=0.23,
                                rationale="僅偵測低信賴微動或疑似玻璃穿透回波",
                            )
                        )

        # 3. VOx Thermal Array BPA
        thm_active = aligned.thermal is not None and privacy_level != PrivacyLevel.L0
        if aligned.thermal is not None:
            thm = aligned.thermal
            if not thm_active:
                bpas.append(
                    ModalityBpa(
                        modality="VOx Thermal",
                        active=False,
                        m_normal=0.0,
                        m_alert=0.0,
                        m_uncertainty=1.0,
                        rationale="L0 關斷待機中（0–5 mW）",
                    )
                )
            else:
                if scenario_id == ScenarioId.C4:
                    if thm.max_temp_c >= 65.0 or thm.temp_rise_rate_c_per_min >= 5.0:
                        bpas.append(
                            ModalityBpa(
                                modality="VOx Thermal",
                                active=True,
                                m_normal=0.04,
                                m_alert=0.86,
                                m_uncertainty=0.10,
                                rationale=f"電芯/接頭異常高溫 {thm.max_temp_c:.1f}°C (斜率 +{thm.temp_rise_rate_c_per_min:.1f}°C/min)",
                            )
                        )
                    else:
                        bpas.append(
                            ModalityBpa(
                                modality="VOx Thermal",
                                active=True,
                                m_normal=0.82,
                                m_alert=0.05,
                                m_uncertainty=0.13,
                                rationale=f"熱場溫度穩定 ({thm.max_temp_c:.1f}°C)",
                            )
                        )
                elif thm.is_glass_boundary_reflection or thm.is_static_heat_source:
                    bpas.append(
                        ModalityBpa(
                            modality="VOx Thermal",
                            active=True,
                            m_normal=0.84,
                            m_alert=0.03,
                            m_uncertainty=0.13,
                            rationale="熱像確認為玻璃反光／太陽炫光或靜態電器熱源",
                        )
                    )
                elif scenario_id == ScenarioId.A1:
                    if thm.human_body_temp_ratio >= 0.45 and 33.5 <= thm.max_temp_c <= 39.5:
                        bpas.append(
                            ModalityBpa(
                                modality="VOx Thermal",
                                active=True,
                                m_normal=0.22,
                                m_alert=0.48,
                                m_uncertainty=0.30,
                                rationale=f"確認人體恆溫熱輪廓 ({thm.max_temp_c:.1f}°C, 佔比 {thm.human_body_temp_ratio:.0%})",
                            )
                        )
                    else:
                        bpas.append(
                            ModalityBpa(
                                modality="VOx Thermal",
                                active=True,
                                m_normal=0.80,
                                m_alert=0.05,
                                m_uncertainty=0.15,
                                rationale="非人體集中熱斑（彌散水蒸氣或冷物/小型寵物排除）",
                            )
                        )
                elif scenario_id in (ScenarioId.B1, ScenarioId.C1):
                    if thm.human_body_temp_ratio >= 0.35 and 32.5 <= thm.max_temp_c <= 40.0:
                        bpas.append(
                            ModalityBpa(
                                modality="VOx Thermal",
                                active=True,
                                m_normal=0.15,
                                m_alert=0.62,
                                m_uncertainty=0.23,
                                rationale=f"確認活體人體體熱目標 ({thm.max_temp_c:.1f}°C)",
                            )
                        )
                    else:
                        bpas.append(
                            ModalityBpa(
                                modality="VOx Thermal",
                                active=True,
                                m_normal=0.84,
                                m_alert=0.03,
                                m_uncertainty=0.13,
                                rationale="無活體體溫特徵（排除電纜、水霧與動物/反光誤報）",
                            )
                        )
                else:  # B2
                    bpas.append(
                        ModalityBpa(
                            modality="VOx Thermal",
                            active=True,
                            m_normal=0.80,
                            m_alert=0.05,
                            m_uncertainty=0.15,
                            rationale=f"室內熱負荷與玻璃邊界正常 ({thm.max_temp_c:.1f}°C)",
                        )
                    )

        # 4. RGB Camera BPA（僅於 L2/L3 啟動於 Volatile RAM 時納入）
        rgb_active = aligned.rgb is not None and privacy_level in (PrivacyLevel.L2, PrivacyLevel.L3)
        if aligned.rgb is not None and rgb_active:
            rgb = aligned.rgb
            if rgb.vehicle_or_glare_heat_mask:
                bpas.append(
                    ModalityBpa(
                        modality="RGB Edge AI (RAM)",
                        active=True,
                        m_normal=0.85,
                        m_alert=0.03,
                        m_uncertainty=0.12,
                        rationale="語意辨識為車輛散熱/環境反光遮罩，排除風險",
                    )
                )
            elif rgb.posture_label == "fallen" or rgb.smoke_or_flame_visual or not rgb.ppe_compliant:
                bpas.append(
                    ModalityBpa(
                        modality="RGB Edge AI (RAM)",
                        active=True,
                        m_normal=0.04,
                        m_alert=0.86,
                        m_uncertainty=0.10,
                        rationale=f"去識別骨架/語意確認高風險 ({rgb.posture_label}, 煙火={rgb.smoke_or_flame_visual})",
                    )
                )
            elif rgb.person_detected:
                alert_val = 0.72 if scenario_id == ScenarioId.B1 else 0.10
                norm_val = 0.14 if scenario_id == ScenarioId.B1 else 0.76
                bpas.append(
                    ModalityBpa(
                        modality="RGB Edge AI (RAM)",
                        active=True,
                        m_normal=norm_val,
                        m_alert=alert_val,
                        m_uncertainty=0.14,
                        rationale=f"17 點骨架姿態確認 ({rgb.posture_label})",
                    )
                )
        else:
            bpas.append(
                ModalityBpa(
                    modality="RGB Edge AI (RAM)",
                    active=False,
                    m_normal=0.0,
                    m_alert=0.0,
                    m_uncertainty=1.0,
                    rationale="Camera-Off by Default（L0/L1 硬體待機，零視覺擷取）",
                )
            )

        return bpas

    def fuse(
        self,
        aligned: AlignedFrame,
        scenario_id: ScenarioId,
        privacy_level: PrivacyLevel,
    ) -> DsFusionResult:
        """從對齊切片建立各模態 BPA 並執行 D-S 正交合併。"""
        bpas = self.compute_modality_bpas(aligned, scenario_id, privacy_level)
        return self.combine_bpas(bpas)
