"""Privacy-by-Design L0–L3 分級喚醒狀態機、硬體形態配置與記憶體清零引擎。

本模組實作四大核心工程機制：
1. 三大硬體部署形態（Phase 1 分散式 MVP / Phase 2 Tier 2 All-in-One / Tier 3 工業級）
   之算力、記憶體頻寬、基準功耗與 PoE 供電預算動態計算。
2. L0 → L1 → L2 → L3 分級狀態轉移與可快進測試之遲滯防抖（Hysteresis 30–60 秒）。
3. 高算力負載下之 Graceful Degradation（降級 RGB 幀率至 10 fps，確保雷達與熱像 20 Hz 追蹤）。
4. TEE Volatile RAM 隔離緩衝區實際覆寫清零計時（< 100 ms，Zero Disk Persistence）。
"""

from __future__ import annotations

import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Optional

from .models import (
    AlignedFrame,
    AuditRecord,
    BandwidthMetrics,
    DsFusionResult,
    HardwareProfile,
    HardwareProfileId,
    LatencyMetrics,
    PowerMetrics,
    PrivacyLevel,
    RiskLevel,
    ScenarioEvaluation,
    ScenarioId,
    SensorActiveStatus,
)

LEVEL_ORDER: dict[PrivacyLevel, int] = {
    PrivacyLevel.L0: 0,
    PrivacyLevel.L1: 1,
    PrivacyLevel.L2: 2,
    PrivacyLevel.L3: 3,
}

PRIVACY_COMPLIANCE_DESC: dict[PrivacyLevel, str] = {
    PrivacyLevel.L0: "Tier-0 最高隱私（零影像 / 零個資，符合 GDPR 非個資範疇）",
    PrivacyLevel.L1: "Tier-1 非視覺合規（僅匿名點雲與粗熱斑，符合 HIPAA/GDPR）",
    PrivacyLevel.L2: "Tier-2 邊緣去識別化（17 點骨架向量，RAM <100ms 清零不落盤）",
    PrivacyLevel.L3: "Tier-3 受控加密存證（AES-256-GCM + TLS 1.3 + RBAC 雙重授權）",
}

HARDWARE_PROFILES: dict[HardwareProfileId, HardwareProfile] = {
    HardwareProfileId.PHASE1_DISTRIBUTED_MVP: HardwareProfile(
        id=HardwareProfileId.PHASE1_DISTRIBUTED_MVP,
        name="Phase 1 分散式利舊 MVP (Edge Box + 無鏡頭 Pod + 既有 ONVIF)",
        architecture_type="分散式 Brownfield 利舊架構",
        wifi_soc="OpenWrt Wi-Fi 6E/7 CSI 測試 AP",
        npu_soc="Rockchip RK3588 (6 TOPS INT8, 34 GB/s)",
        radar_chip="TI IWRL6432 / IWR6843AOP (60 GHz)",
        thermal_chip="Melexis MLX90640 (32×24) / FLIR Lepton 3.5",
        camera_sensor="既有 ONVIF/RTSP IP Camera (Warm-Standby)",
        npu_tops=6.0,
        memory_bw_gbps=34.0,
        ap_base_power_w=14.0,
        poe_standard="PoE+ (IEEE 802.3at 30W) + 分散式 PoE 埠分攤",
        poe_budget_w=25.5,
        thermal_fps_cap=9.0,
        graceful_degrade_load_pct=80.0,
        bom_range_usd="$141–$174 USD（分散式單點攤提）",
    ),
    HardwareProfileId.PHASE2_ALL_IN_ONE_TIER2: HardwareProfile(
        id=HardwareProfileId.PHASE2_ALL_IN_ONE_TIER2,
        name="Phase 2 All-in-One 吸頂智慧 AP (Tier 2 中高階商用)",
        architecture_type="All-in-One 吸頂／壁掛雲台一體機",
        wifi_soc="Qualcomm IPQ5424 / MediaTek Filogic 880 (Wi-Fi 7 MLO)",
        npu_soc="Qualcomm QCS6490 (12 TOPS) / Ambarella CV72",
        radar_chip="TI IWR6843AOP (60 GHz 3Tx/4Rx AoP)",
        thermal_chip="FLIR Lepton 3.5 (160×120 NDAA) / InfiRay Tiny1-C",
        camera_sensor="Sony IMX415 / IMX678 (4K Starlight CMOS)",
        npu_tops=12.0,
        memory_bw_gbps=51.0,
        ap_base_power_w=20.5,
        poe_standard="PoE++ Type 3 (IEEE 802.3bt 60W)",
        poe_budget_w=51.0,
        thermal_fps_cap=9.0,
        graceful_degrade_load_pct=85.0,
        bom_range_usd="$189–$260 (非 NDAA) / $257–$355 USD (NDAA 合規)",
    ),
    HardwareProfileId.TIER3_INDUSTRIAL: HardwareProfile(
        id=HardwareProfileId.TIER3_INDUSTRIAL,
        name="Tier 3 工業級關鍵基礎設施節點 (IP67 寬溫 -40°C~+85°C)",
        architecture_type="工業級 All-in-One / 分散式高算力閘道",
        wifi_soc="Qualcomm IPQ9574 + QCN9274 / Filogic 880 (Wi-Fi 7 10G)",
        npu_soc="NVIDIA Jetson Orin NX 8GB/16GB (70–100 TOPS, 102.4 GB/s)",
        radar_chip="TI AWR2944 (77 GHz) / 雙 IWR6843 (60 GHz 協同)",
        thermal_chip="Seek Mosaic 320 / FLIR Boson 640 (輻射測溫 + 黑體校準)",
        camera_sensor="Sony IMX678 4K WDR STARVIS 2",
        npu_tops=70.0,
        memory_bw_gbps=102.4,
        ap_base_power_w=26.0,
        poe_standard="PoE++ Type 4 (IEEE 802.3bt 90W)",
        poe_budget_w=71.3,
        thermal_fps_cap=30.0,
        graceful_degrade_load_pct=90.0,
        bom_range_usd="$649–$960 (準工業) / $2,314–$3,695 USD (旗艦)",
    ),
}


class VolatileRamBuffer:
    """模擬 TEE 隔離之揮發性記憶體（Volatile RAM）影格環形緩衝區。

    嚴格禁止寫入任何永久儲存（Zero Disk Persistence），並在 L2 推論取出
    17 點骨架向量後執行強制位元組覆寫清零與全零校驗（保證耗時 < 100 ms）。
    """

    def __init__(self, buffer_bytes: int = 262_144) -> None:
        self._size = buffer_bytes
        self._buf = bytearray(buffer_bytes)
        self._has_sensitive_data = False
        self.last_wipe_ms: float = 0.0
        self.last_wipe_verified: bool = True

    def stage_raw_yuv_frame(self, seed_byte: int = 0xA5) -> None:
        """模擬將攝影機 YUV 原始影格 DMA 寫入 TEE Volatile RAM（不落盤）。"""
        # 填充非零測試樣態以確保後續覆寫與全零校驗具備真實效力
        pattern = bytes([(seed_byte + i) & 0xFF or 0x5A for i in range(64)])
        repeats = self._size // len(pattern)
        rem = self._size % len(pattern)
        self._buf[: repeats * len(pattern)] = pattern * repeats
        if rem:
            self._buf[repeats * len(pattern) :] = pattern[:rem]
        self._has_sensitive_data = True
        self.last_wipe_verified = False

    def wipe_and_verify(self) -> tuple[float, bool]:
        """於推論後即時將整塊 RAM 緩衝區覆寫為 0x00 並驗證全零，回傳 (耗時ms, 驗證結果)。"""
        t0 = time.perf_counter()
        self._buf[:] = b"\x00" * self._size
        # 嚴格驗證緩衝區不存在任何非零位元組
        is_all_zero = not any(self._buf)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self._has_sensitive_data = not is_all_zero
        self.last_wipe_ms = round(max(0.01, elapsed_ms), 4)
        self.last_wipe_verified = is_all_zero
        return (self.last_wipe_ms, is_all_zero)

    @property
    def buffer_bytes(self) -> int:
        return self._size

    def read_raw_snapshot(self) -> bytes:
        """僅供內部測試檢查目前緩衝區是否殘留非零內容。"""
        return bytes(self._buf[:256])


class PrivacyStateMachine:
    """Privacy-by-Design 四階（L0–L3）動態喚醒狀態機與資源計量器。"""

    BASELINE_4K_KBPS: float = 6000.0  # 全時 4K H.265 上行對照基線 (6 Mbps)

    def __init__(
        self,
        profile_id: HardwareProfileId = HardwareProfileId.PHASE2_ALL_IN_ONE_TIER2,
        hysteresis_s: float = 35.0,
        critical_confirm_frames: int = 3,
    ) -> None:
        self.profile: HardwareProfile = HARDWARE_PROFILES[profile_id]
        self.current_level: PrivacyLevel = PrivacyLevel.L0
        self.hysteresis_required_s: float = hysteresis_s
        self.critical_confirm_frames: int = critical_confirm_frames

        self.sim_time_s: float = 0.0
        self._downgrade_target: Optional[PrivacyLevel] = None
        self._downgrade_start_s: Optional[float] = None
        self._consecutive_critical_frames: int = 0

        self.l3_authorized: bool = False
        self.l3_operator_id: Optional[str] = None

        self.burst_load_injected: bool = False
        self.graceful_degradation_active: bool = False
        self.npu_load_pct: float = 3.5

        self.ram_buffer = VolatileRamBuffer()
        self.last_ram_wipe_ms: float = 0.0
        self.zero_disk_persistence: bool = True

        self._audits: deque[AuditRecord] = deque(maxlen=60)
        self._record_audit(
            event_type="SYSTEM_INIT",
            scenario_id="A1",
            detail=f"狀態機初始化完成，預設硬體藍本：{self.profile.name}，Camera-Off by Default (L0)",
        )

    def set_hardware_profile(self, profile_id: HardwareProfileId) -> HardwareProfile:
        """熱切換三大硬體部署形態並更新算力與 PoE 預算。"""
        self.profile = HARDWARE_PROFILES[profile_id]
        self._record_audit(
            event_type="HARDWARE_SWITCH",
            scenario_id="SYSTEM",
            detail=f"切換硬體形態為 {self.profile.name}（NPU {self.profile.npu_tops} TOPS, PoE 上限 {self.profile.poe_budget_w}W）",
        )
        return self.profile

    def set_burst_load(self, enabled: bool) -> None:
        """啟閉高併發 NPU 滿載壓力模擬，以驗證 Graceful Degradation 降幀保護。"""
        self.burst_load_injected = enabled
        self._record_audit(
            event_type="NPU_STRESS_TOGGLE",
            scenario_id="SYSTEM",
            detail="開啟高併發多目標算力壓力模擬" if enabled else "解除算力壓力模擬，恢復標準幀率",
        )

    def authorize_l3(
        self,
        operator_id: str,
        otp_token: str,
        action: str = "grant",
        reason: str = "雙因子人工覆核",
        scenario_id: str = "SYSTEM",
    ) -> tuple[bool, str]:
        """模擬 L3 雙因子 RBAC + OTP 授權啟封存證或解除警報。"""
        clean_otp = otp_token.strip()
        if len(clean_otp) != 6 or not clean_otp.isdigit() or clean_otp == "000000":
            self._record_audit(
                event_type="L3_AUTH_REJECTED",
                scenario_id=scenario_id,
                detail=f"操作員 {operator_id} OTP 雙因子權杖驗證失敗",
            )
            return False, "OTP 雙因子動態權杖無效（須為非全零 6 位數字）"

        if action == "revoke":
            self.l3_authorized = False
            self.l3_operator_id = None
            self._consecutive_critical_frames = 0
            self.current_level = PrivacyLevel.L1
            self._downgrade_target = None
            self._downgrade_start_s = None
            self._record_audit(
                event_type="L3_AUTH_REVOKED",
                scenario_id=scenario_id,
                detail=f"操作員 {operator_id} 已完成覆核並解除緊急存證鎖定，退回 L1 非視覺防護（事由：{reason}）",
                northbound_action="L3_FORENSIC_STREAM_CLOSED",
            )
            return True, "已解除 L3 緊急授權並關閉加密視訊串流"

        self.l3_authorized = True
        self.l3_operator_id = operator_id
        self.current_level = PrivacyLevel.L3
        self._downgrade_target = None
        self._downgrade_start_s = None
        self._record_audit(
            event_type="L3_AUTH_GRANTED",
            scenario_id=scenario_id,
            detail=f"操作員 {operator_id} 通過 RBAC+OTP 雙因子授權啟封 L3 AES-256-GCM Pre-roll 存證（事由：{reason}）",
            northbound_action="AES256_GCM_PREROLL_STREAM_OPENED",
        )
        return True, "雙因子授權通過，已啟封 L3 AES-256-GCM 加密存證與雙向通報"

    def _determine_desired_level(
        self,
        aligned: AlignedFrame,
        ds_result: DsFusionResult,
        evaluation: ScenarioEvaluation,
    ) -> PrivacyLevel:
        """根據多模態 D-S 信念、高衝突指標與場景評估決定期望隱私等級。"""
        if self.l3_authorized:
            return PrivacyLevel.L3

        desired = evaluation.recommended_privacy_level

        # 若 D-S 出現高衝突（K >= 0.99），強制升階至 L2 進行邊緣語意仲裁（除非為日間 B2 節能）
        if ds_result.high_conflict_flag and evaluation.scenario_id != ScenarioId.B2:
            if LEVEL_ORDER[desired] < LEVEL_ORDER[PrivacyLevel.L2]:
                desired = PrivacyLevel.L2

        # 若連續多幀確認 CRITICAL 危急事件，才自動從 L2 跨入 L3（防止單幀毛刺誤洩視訊）
        if evaluation.risk_level == RiskLevel.CRITICAL:
            self._consecutive_critical_frames += 1
            if self._consecutive_critical_frames >= self.critical_confirm_frames:
                desired = PrivacyLevel.L3
            elif LEVEL_ORDER[desired] > LEVEL_ORDER[PrivacyLevel.L2]:
                desired = PrivacyLevel.L2
        else:
            self._consecutive_critical_frames = 0

        # 日間商辦 HVAC 在席確認強制維持 L1 Camera-Off
        if evaluation.scenario_id == ScenarioId.B2 and evaluation.risk_level == RiskLevel.SAFE:
            if LEVEL_ORDER[desired] > LEVEL_ORDER[PrivacyLevel.L1]:
                desired = PrivacyLevel.L1

        return desired

    def step(
        self,
        aligned: AlignedFrame,
        ds_result: DsFusionResult,
        evaluation: ScenarioEvaluation,
        dt_s: float = 0.125,
        override_sim_time_s: Optional[float] = None,
    ) -> dict:
        """推進單步狀態機演算，處理升降級防抖、RAM 清零與降載保護。"""
        if override_sim_time_s is not None:
            self.sim_time_s = override_sim_time_s
        else:
            self.sim_time_s = round(self.sim_time_s + max(0.0, dt_s), 4)

        prev_level = self.current_level
        desired = self._determine_desired_level(aligned, ds_result, evaluation)

        # 狀態升階（即時反應）或降階（遲滯防抖 Hysteresis）
        if LEVEL_ORDER[desired] > LEVEL_ORDER[self.current_level]:
            self.current_level = desired
            self._downgrade_target = None
            self._downgrade_start_s = None
            self._record_audit(
                event_type="STATE_ESCALATE",
                scenario_id=evaluation.scenario_id.value,
                detail=f"隱私狀態由 {prev_level.value} 升階至 {self.current_level.value}（風險={evaluation.risk_level.value}, Bel(Alert)={ds_result.belief_alert:.2f}）",
                northbound_action=evaluation.northbound_commands[0]["action"]
                if evaluation.northbound_commands
                else None,
            )
        elif LEVEL_ORDER[desired] < LEVEL_ORDER[self.current_level]:
            # L3 在雙因子手動授權期間不自動降級，須人工 revoke
            if not (self.current_level == PrivacyLevel.L3 and self.l3_authorized):
                if self._downgrade_target != desired or self._downgrade_start_s is None:
                    self._downgrade_target = desired
                    self._downgrade_start_s = self.sim_time_s
                elapsed = self.sim_time_s - self._downgrade_start_s
                if elapsed >= self.hysteresis_required_s:
                    self.current_level = desired
                    self._downgrade_target = None
                    self._downgrade_start_s = None
                    self._record_audit(
                        event_type="STATE_DOWNGRADE",
                        scenario_id=evaluation.scenario_id.value,
                        detail=f"防抖冷卻滿 {self.hysteresis_required_s:.0f}s，狀態安全降階 {prev_level.value} → {self.current_level.value}",
                    )
        else:
            self._downgrade_target = None
            self._downgrade_start_s = None

        # 計算 NPU 負載與 Graceful Degradation
        base_load = {
            PrivacyLevel.L0: 3.0,
            PrivacyLevel.L1: 14.0,
            PrivacyLevel.L2: 56.0,
            PrivacyLevel.L3: 72.0,
        }[self.current_level]
        # 依硬體 NPU 算力縮放（6 TOPS 相較 12 TOPS 負載更高；70 TOPS 極餘裕）
        tops_scale = 12.0 / max(1.0, self.profile.npu_tops)
        computed_load = base_load * (0.55 + 0.45 * min(1.8, tops_scale))
        if self.burst_load_injected:
            computed_load += 38.0
        self.npu_load_pct = round(min(99.5, max(2.0, computed_load)), 1)

        degrade_active = (
            self.npu_load_pct >= self.profile.graceful_degrade_load_pct
            and self.current_level in (PrivacyLevel.L2, PrivacyLevel.L3)
        )
        if degrade_active != self.graceful_degradation_active:
            self._record_audit(
                event_type="GRACEFUL_DEGRADATION",
                scenario_id=evaluation.scenario_id.value,
                detail=(
                    f"NPU 負載達 {self.npu_load_pct}%（門檻 {self.profile.graceful_degrade_load_pct}%），RGB 降載至 10 fps 優先保障雷達/熱像 20 Hz 追蹤"
                    if degrade_active
                    else "NPU 負載恢復安全區間，解除降幀保護"
                ),
            )
        self.graceful_degradation_active = degrade_active

        # L2/L3 執行 Volatile RAM 隔離寫入 + 骨架提取 + 強制覆寫清零驗證
        if self.current_level in (PrivacyLevel.L2, PrivacyLevel.L3):
            self.ram_buffer.stage_raw_yuv_frame()
            wipe_ms, verified = self.ram_buffer.wipe_and_verify()
            self.last_ram_wipe_ms = wipe_ms
            self.zero_disk_persistence = verified
            if aligned.rgb is not None:
                aligned.rgb.ram_wipe_duration_ms = wipe_ms
                aligned.rgb.raw_frame_wiped = verified
                aligned.rgb.captured_in_ram_only = True
                aligned.rgb.encrypted_forensic_ready = self.current_level == PrivacyLevel.L3
            if prev_level != self.current_level or len(self._audits) == 0:
                self._record_audit(
                    event_type="VOLATILE_RAM_WIPE",
                    scenario_id=evaluation.scenario_id.value,
                    ram_wipe_ms=wipe_ms,
                    zero_disk_verified=verified,
                    detail=f"L2/L3 擷取 17 點骨架後完成 Volatile RAM 覆寫清零（耗時 {wipe_ms:.3f} ms < 100 ms，Zero Disk={verified}）",
                )
        else:
            # L0/L1 嚴格實施 Camera-Off by Default：不觸發擷取，不保留任何骨架影像
            self.last_ram_wipe_ms = self.ram_buffer.last_wipe_ms
            if aligned.rgb is not None:
                aligned.rgb.captured_in_ram_only = False
                aligned.rgb.skeleton_17pts = []
                aligned.rgb.person_detected = False
                aligned.rgb.raw_frame_wiped = True
                aligned.rgb.encrypted_forensic_ready = False

        # 若有新的北向觸發命令且剛進入高風險，補記一筆稽核
        if evaluation.northbound_commands and (
            prev_level != self.current_level or evaluation.risk_level in (RiskLevel.HIGH, RiskLevel.CRITICAL)
        ):
            top_cmd = evaluation.northbound_commands[0]
            if not self._audits or self._audits[0].northbound_action != top_cmd["action"]:
                self._record_audit(
                    event_type="NORTHBOUND_DISPATCH",
                    scenario_id=evaluation.scenario_id.value,
                    detail=f"[{top_cmd['protocol']}] 下發至 {top_cmd['target']}：{top_cmd['action']}",
                    northbound_action=top_cmd["action"],
                )

        hyst_elapsed = (
            round(self.sim_time_s - self._downgrade_start_s, 2)
            if self._downgrade_start_s is not None
            else 0.0
        )

        return {
            "hysteresis_target_level": self._downgrade_target,
            "hysteresis_elapsed_s": hyst_elapsed,
        }

    def compute_metrics(self) -> tuple[SensorActiveStatus, BandwidthMetrics, PowerMetrics, LatencyMetrics]:
        """依據當前狀態與硬體配置精確計算四感測器幀率、頻寬、功耗與延遲。"""
        lvl = self.current_level
        thm_cap = self.profile.thermal_fps_cap
        degraded = self.graceful_degradation_active

        if lvl == PrivacyLevel.L0:
            sensors = SensorActiveStatus(
                csi_hz=100.0,
                mmwave_hz=0.0,
                mmwave_mode="LPDS 深度休眠 (~0.5 mW, 2ms 喚醒)",
                thermal_hz=0.0,
                thermal_mode="關斷 / Shutdown (0–5 mW)",
                rgb_fps=0.0,
                rgb_mode="Camera-Off 硬體待機 (<5 mW)",
            )
            bw = BandwidthMetrics(
                internal_bus_mbps=0.85,
                uplink_kbps=3.2,
                uplink_mbps=0.0032,
                saving_vs_4k_pct=round((1.0 - 3.2 / self.BASELINE_4K_KBPS) * 100.0, 2),
            )
            inc_w = 0.55
            lat = LatencyMetrics(
                stage0_rf_ms=28.0,
                stage1_l1_wakeup_ms=0.0,
                stage2_rgb_align_ms=0.0,
                stage3_npu_infer_ms=0.0,
                stage4_encrypt_tx_ms=2.0,
                total_e2e_ms=30.0,
                warm_standby_enabled=True,
            )
        elif lvl == PrivacyLevel.L1:
            sensors = SensorActiveStatus(
                csi_hz=150.0,
                mmwave_hz=10.0,
                mmwave_mode="10 Hz 3D 軌跡與呼吸微多普勒",
                thermal_hz=min(9.0, thm_cap),
                thermal_mode=f"{min(9.0, thm_cap):.0f} Hz 低功耗粗粒度熱異常掃描",
                rgb_fps=0.0,
                rgb_mode="Camera-Off 硬體待機 (<5 mW)",
            )
            bw = BandwidthMetrics(
                internal_bus_mbps=2.6,
                uplink_kbps=14.5,
                uplink_mbps=0.0145,
                saving_vs_4k_pct=round((1.0 - 14.5 / self.BASELINE_4K_KBPS) * 100.0, 2),
            )
            inc_w = 1.85
            lat = LatencyMetrics(
                stage0_rf_ms=26.0,
                stage1_l1_wakeup_ms=28.0,
                stage2_rgb_align_ms=0.0,
                stage3_npu_infer_ms=6.0,
                stage4_encrypt_tx_ms=4.0,
                total_e2e_ms=64.0,
                warm_standby_enabled=True,
            )
        elif lvl == PrivacyLevel.L2:
            rgb_fps = 10.0 if degraded else 30.0
            thm_hz = min(20.0 if degraded else 15.0, thm_cap)
            sensors = SensorActiveStatus(
                csi_hz=500.0,
                mmwave_hz=20.0,
                mmwave_mode="20 Hz 高密度點雲（降載保護鎖定）" if degraded else "20 Hz 高密度 3D 點雲",
                thermal_hz=thm_hz,
                thermal_mode=f"{thm_hz:.0f} Hz 輻射測溫熱圖",
                rgb_fps=rgb_fps,
                rgb_mode="10 fps Graceful Degradation (TEE RAM)" if degraded else "30 fps 熱喚醒進 TEE RAM（不落盤）",
            )
            bus_mbps = 460.0 if degraded else 1180.0
            bw = BandwidthMetrics(
                internal_bus_mbps=bus_mbps,
                uplink_kbps=38.4,
                uplink_mbps=0.0384,
                saving_vs_4k_pct=round((1.0 - 38.4 / self.BASELINE_4K_KBPS) * 100.0, 2),
            )
            inc_w = 7.4 if self.profile.id != HardwareProfileId.TIER3_INDUSTRIAL else 9.8
            npu_ms = 24.0 if self.profile.npu_tops <= 6.5 else (16.0 if self.profile.npu_tops <= 15.0 else 11.0)
            if degraded:
                npu_ms += 14.0
            lat = LatencyMetrics(
                stage0_rf_ms=25.0,
                stage1_l1_wakeup_ms=24.0,
                stage2_rgb_align_ms=38.0,
                stage3_npu_infer_ms=npu_ms,
                stage4_encrypt_tx_ms=12.0,
                total_e2e_ms=round(25.0 + 24.0 + 38.0 + npu_ms + 12.0, 1),
                warm_standby_enabled=True,
            )
        else:  # L3
            rgb_fps = 10.0 if degraded else 30.0
            thm_hz = min(20.0 if degraded else 30.0, thm_cap)
            sensors = SensorActiveStatus(
                csi_hz=1000.0,
                mmwave_hz=20.0 if degraded else 25.0,
                mmwave_mode="20 Hz 降載鎖定追蹤" if degraded else "25 Hz 全速點雲與搜救追蹤",
                thermal_hz=thm_hz,
                thermal_mode=f"{thm_hz:.0f} Hz 全速測溫矩陣",
                rgb_fps=rgb_fps,
                rgb_mode="10 fps 降載保護 H.265 加密" if degraded else "30 fps H.265 + AES-256-GCM 授權存證",
            )
            bw = BandwidthMetrics(
                internal_bus_mbps=1450.0 if degraded else 2450.0,
                uplink_kbps=16800.0 if not degraded else 8200.0,
                uplink_mbps=16.8 if not degraded else 8.2,
                saving_vs_4k_pct=0.0,
            )
            inc_w = 11.2 if self.profile.id != HardwareProfileId.TIER3_INDUSTRIAL else 14.2
            npu_ms = 28.0 if self.profile.npu_tops <= 6.5 else (18.0 if self.profile.npu_tops <= 15.0 else 12.0)
            if degraded:
                npu_ms += 18.0
            lat = LatencyMetrics(
                stage0_rf_ms=24.0,
                stage1_l1_wakeup_ms=22.0,
                stage2_rgb_align_ms=40.0,
                stage3_npu_infer_ms=npu_ms,
                stage4_encrypt_tx_ms=22.0,
                total_e2e_ms=round(24.0 + 22.0 + 40.0 + npu_ms + 22.0, 1),
                warm_standby_enabled=True,
            )

        total_w = round(self.profile.ap_base_power_w + inc_w, 2)
        poe_pct = round((total_w / self.profile.poe_budget_w) * 100.0, 1)
        pwr = PowerMetrics(
            sensor_npu_incremental_w=round(inc_w, 2),
            ap_base_w=self.profile.ap_base_power_w,
            total_system_w=total_w,
            poe_budget_w=self.profile.poe_budget_w,
            poe_utilization_pct=poe_pct,
            poe_warning=poe_pct >= 92.0,
        )
        return sensors, bw, pwr, lat

    def _record_audit(
        self,
        event_type: str,
        scenario_id: str,
        detail: str,
        ram_wipe_ms: Optional[float] = None,
        zero_disk_verified: bool = True,
        northbound_action: Optional[str] = None,
    ) -> None:
        rec = AuditRecord(
            id=uuid.uuid4().hex[:10],
            timestamp_iso=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            sim_time_s=round(self.sim_time_s, 2),
            event_type=event_type,
            privacy_level=self.current_level,
            scenario_id=scenario_id,
            ram_wipe_ms=ram_wipe_ms,
            zero_disk_verified=zero_disk_verified,
            detail=detail,
            northbound_action=northbound_action,
        )
        self._audits.appendleft(rec)

    def get_audits(self, limit: int = 30) -> list[AuditRecord]:
        """取得最新隱私清零與北向聯動稽核日誌清單。"""
        return list(self._audits)[:limit]
