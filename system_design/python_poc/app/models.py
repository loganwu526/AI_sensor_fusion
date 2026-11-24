"""四模態感測融合與 Privacy-by-Design 狀態機資料模型定義。

本模組定義所有感測幀（Wi-Fi CSI / mmWave / Thermal / RGB 去識別化骨架）、
Dempster-Shafer (D-S) 證據融合輸出、三大硬體部署形態設定、L0–L3 狀態機快照，
以及五大旗艦場景模組回傳之評估結構。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


class PrivacyLevel(str, Enum):
    """Privacy-by-Design 四階隱私狀態等級。"""

    L0 = "L0"  # 常駐極低功耗無線環境感知（Camera-Off）
    L1 = "L1"  # 非視覺雙重物理驗證（mmWave + 低階 Thermal，Camera-Off）
    L2 = "L2"  # Edge AI 去識別化視覺骨架推論（Volatile RAM <100ms 銷毀）
    L3 = "L3"  # 經雙重授權或危急告警觸發之加密存證與雙向通報


class RiskLevel(str, Enum):
    """場景評估風險等級。"""

    SAFE = "SAFE"
    LOW = "LOW"
    ELEVATED = "ELEVATED"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class HardwareProfileId(str, Enum):
    """三大核准硬體部署形態識別碼。"""

    PHASE1_DISTRIBUTED_MVP = "phase1_distributed_mvp"
    PHASE2_ALL_IN_ONE_TIER2 = "phase2_all_in_one_tier2"
    TIER3_INDUSTRIAL = "tier3_industrial"


class ScenarioId(str, Enum):
    """五大可插拔旗艦應用場景識別碼。"""

    A1 = "A1"  # 銀髮居家／長照衛浴跌倒與生命徵象
    C4 = "C4"  # BESS 儲能站／EV 超充熱失控截斷與濃煙搜救
    B1 = "B1"  # 關鍵園區／倉儲全天候零誤報周界入侵
    C1 = "C1"  # 智慧工廠人機協作（HRC）／粉塵盲區防撞
    B2 = "B2"  # 智慧商辦微氣候 HVAC 零 False-Off 與夜間安防


class SourceMode(str, Enum):
    """系統感測資料來源運作模式。"""

    SIMULATOR = "simulator"        # 合成情境模擬器（即時演示控制）
    LOG_REPLAY = "log_replay"      # 公開／實測時間軸資料集回放
    HYBRID_LIVE = "hybrid_live"    # 混合實況（資料集回放 + 真實攝影機隱私閘控）


class HardwareProfile(BaseModel):
    """硬體部署形態與電源／算力規格配置。"""

    id: HardwareProfileId
    name: str = Field(..., description="硬體藍本名稱")
    architecture_type: str = Field(..., description="部署架構分類")
    wifi_soc: str = Field(..., description="Wi-Fi / 網通與無線感測主晶片")
    npu_soc: str = Field(..., description="邊緣 AI 運算平台與 TOPS 規格")
    radar_chip: str = Field(..., description="毫米波雷達晶片型號")
    thermal_chip: str = Field(..., description="遠紅外線熱成像晶片模組")
    camera_sensor: str = Field(..., description="光學 CMOS 攝影機規格")
    npu_tops: float = Field(..., gt=0, description="Edge NPU INT8 算力上限（TOPS）")
    memory_bw_gbps: float = Field(..., gt=0, description="記憶體匯流排頻寬（GB/s）")
    ap_base_power_w: float = Field(..., gt=0, description="含 AP 骨幹基礎運行功耗（W）")
    poe_standard: str = Field(..., description="IEEE 802.3 PoE 供電規範")
    poe_budget_w: float = Field(..., gt=0, description="PD 端可用供電功率上限（W）")
    thermal_fps_cap: float = Field(..., gt=0, description="熱像儀最大允許更新率（Hz）")
    graceful_degrade_load_pct: float = Field(
        ..., ge=50.0, le=99.0, description="觸發 Graceful Degradation 降幀保護之 NPU 負載門檻（%）"
    )
    bom_range_usd: str = Field(..., description="工程估算 BOM 成本區間")


class CsiFrame(BaseModel):
    """Wi-Fi 802.11bf CSI 與 BLE AoA / UWB 射頻感測幀（100–1000 Hz）。"""

    timestamp_ms: float = Field(..., description="IEEE 1588 PTP 對齊時間戳（毫秒）")
    subcarrier_amplitudes: list[float] = Field(
        default_factory=list, description="30 個子載波正規化振幅分佈"
    )
    phase_diffs: list[float] = Field(
        default_factory=list, description="天線對 CSI 相位差序列（rad）"
    )
    fresnel_variance: float = Field(0.0, ge=0.0, description="Fresnel 區動態擾動方差")
    doppler_energy: float = Field(0.0, ge=0.0, description="射頻微多普勒動態能量")
    motion_score: float = Field(0.0, ge=0.0, le=1.0, description="常態化無線移動活躍度")
    ble_whitelist_present: bool = Field(False, description="BLE AoA 授權白名單標籤在場")
    uwb_tag_distance_m: Optional[float] = Field(None, description="UWB ToF 標籤測距（公尺）")
    uwb_authorized: bool = Field(False, description="UWB IEEE 802.15.4z STS 白名單授權狀態")
    uwb_nlos_alert: bool = Field(False, description="UWB 跨貨架／跨牆非視距盲區逼近預警")


class MmWavePoint(BaseModel):
    """60/77 GHz mmWave 單一 3D 速度點雲點位。"""

    x: float = Field(..., description="橫向位置 X（公尺）")
    y: float = Field(..., description="縱深距離 Y（公尺）")
    z: float = Field(..., description="離地高度 Z（公尺）")
    vx: float = Field(0.0, description="X 軸速度（m/s）")
    vy: float = Field(0.0, description="Y 軸速度（m/s）")
    vz: float = Field(0.0, description="垂直 Z 軸速度（m/s）")
    snr: float = Field(15.0, description="訊噪比（dB）")
    rcs: float = Field(0.5, description="雷達截面積估算")


class MmWaveFrame(BaseModel):
    """mmWave FMCW 3D 點雲與微多普勒生理感測幀（10–20 Hz）。"""

    timestamp_ms: float = Field(..., description="IEEE 1588 PTP 時間戳（毫秒）")
    points: list[MmWavePoint] = Field(default_factory=list, description="3D 速度點雲清單")
    centroid_z_m: float = Field(1.2, description="目標點雲質心離地高度（公尺）")
    vz_mps: float = Field(0.0, description="目標垂直速度分量（m/s，負值為向下墜落）")
    horizontal_speed_mps: float = Field(0.0, ge=0.0, description="水平合成移動速度（m/s）")
    breathing_bpm: float = Field(16.0, ge=0.0, description="胸腔微動估測呼吸率（次/分）")
    heart_rate_bpm: float = Field(72.0, ge=0.0, description="估測心率（次/分）")
    breathing_confidence: float = Field(0.0, ge=0.0, le=1.0, description="呼吸微多普勒信賴度")
    micro_doppler_human_score: float = Field(
        0.0, ge=0.0, le=1.0, description="人體步態／肢體微多普勒特徵匹配度"
    )
    smoke_penetration_active: bool = Field(False, description="濃煙／粉塵穿透追蹤模式啟用旗標")
    target_distance_m: float = Field(3.0, ge=0.0, description="主要目標距離（公尺）")


class ThermalFrame(BaseModel):
    """非製冷 VOx 遠紅外線熱成像陣列幀（9–30 Hz）。"""

    timestamp_ms: float = Field(..., description="IEEE 1588 PTP 時間戳（毫秒）")
    width: int = Field(16, ge=1)
    height: int = Field(12, ge=1)
    grid_flat: list[float] = Field(default_factory=list, description="攤平熱陣列溫度矩陣（°C）")
    max_temp_c: float = Field(25.0, description="畫面最高表面溫度（°C）")
    min_temp_c: float = Field(22.0, description="畫面最低環境溫度（°C）")
    ambient_temp_c: float = Field(24.0, description="背景基準環境溫度（°C）")
    human_body_temp_ratio: float = Field(
        0.0, ge=0.0, le=1.0, description="符合人體體表溫度窗（33–38.5°C）像素群比例"
    )
    temp_rise_rate_c_per_min: float = Field(0.0, description="關鍵熱點溫升斜率（°C/min）")
    hotspot_x: float = Field(0.5, ge=0.0, le=1.0, description="熱點正規化 X 座標")
    hotspot_y: float = Field(0.5, ge=0.0, le=1.0, description="熱點正規化 Y 座標")
    is_static_heat_source: bool = Field(False, description="是否為無呼吸／無位移之靜態發熱電器")
    is_glass_boundary_reflection: bool = Field(
        False, description="是否判定為玻璃隔間反射或太陽強光熱反光"
    )


class SkeletonKeypoint(BaseModel):
    """L2 去識別化單一關節點座標（僅含幾何拓撲，不含人臉紋理）。"""

    name: str
    x: float = Field(..., ge=0.0, le=1.0)
    y: float = Field(..., ge=0.0, le=1.0)
    conf: float = Field(0.9, ge=0.0, le=1.0)


class RgbFrameMetadata(BaseModel):
    """IP Camera 視覺推論元數據（L0/L1 預設關閉；L2 僅於 Volatile RAM 短暫駐留並立即清零）。"""

    timestamp_ms: float = Field(..., description="IEEE 1588 PTP 時間戳（毫秒）")
    captured_in_ram_only: bool = Field(False, description="影像僅於 Volatile RAM 推論（Zero Disk）")
    person_detected: bool = Field(False, description="是否偵測到人體目標")
    posture_label: str = Field("none", description="去識別化姿態標籤（standing/sitting/fallen/none）")
    fall_aspect_ratio: float = Field(0.4, ge=0.0, description="邊界框寬高比（>1.2 常代表水平倒地）")
    smoke_or_flame_visual: bool = Field(False, description="視覺端是否觀察到煙霧或明火特徵")
    ppe_compliant: bool = Field(True, description="作業人員安全帽與反光背心穿戴合規")
    vehicle_or_glare_heat_mask: bool = Field(
        False, description="視覺語意是否辨識為車輛引擎蓋／煞車盤散熱或陽光反光遮罩"
    )
    skeleton_17pts: list[SkeletonKeypoint] = Field(
        default_factory=list, description="COCO 17 點去識別化骨架向量"
    )
    raw_frame_wiped: bool = Field(True, description="原始 YUV/RGB 記憶體緩衝區已覆寫清零驗證")
    ram_wipe_duration_ms: float = Field(0.0, ge=0.0, description="Volatile RAM 覆寫清零實測耗時（ms）")
    encrypted_forensic_ready: bool = Field(
        False, description="L3 AES-256-GCM 加密 Pre-roll 存證片段備便旗標"
    )


class AlignedFrame(BaseModel):
    """經 100 ms 滑動視窗與卡爾曼速度外推後之時空對齊多模態切片。"""

    target_timestamp_ms: float
    window_ms: float = 100.0
    csi: Optional[CsiFrame] = None
    mmwave: Optional[MmWaveFrame] = None
    thermal: Optional[ThermalFrame] = None
    rgb: Optional[RgbFrameMetadata] = None
    csi_offset_ms: float = 0.0
    mmwave_offset_ms: float = 0.0
    thermal_offset_ms: float = 0.0
    rgb_offset_ms: float = 0.0
    mmwave_extrapolated: bool = False
    extrapolated_position: tuple[float, float, float] = (0.0, 0.0, 0.0)
    alignment_quality: float = Field(1.0, ge=0.0, le=1.0)


class ModalityBpa(BaseModel):
    """單一模態基本機率分配函數（Basic Probability Assignment, BPA）。"""

    modality: str
    active: bool = True
    m_normal: float = Field(..., ge=0.0, le=1.0)
    m_alert: float = Field(..., ge=0.0, le=1.0)
    m_uncertainty: float = Field(..., ge=0.0, le=1.0)
    rationale: str = ""


class DsFusionResult(BaseModel):
    """Dempster-Shafer (D-S) 證據正交合併結果與衝突指標。"""

    m_normal: float = Field(..., ge=0.0, le=1.0)
    m_alert: float = Field(..., ge=0.0, le=1.0)
    m_uncertainty: float = Field(..., ge=0.0, le=1.0)
    conflict_k: float = Field(..., ge=0.0, le=1.0, description="Dempster 衝突係數 K")
    belief_alert: float = Field(..., ge=0.0, le=1.0, description="告警信念函數 Bel(Alert)")
    plausibility_alert: float = Field(..., ge=0.0, le=1.0, description="告警似然函數 Pl(Alert)")
    belief_normal: float = Field(..., ge=0.0, le=1.0, description="正常信念函數 Bel(Normal)")
    plausibility_normal: float = Field(..., ge=0.0, le=1.0, description="正常似然函數 Pl(Normal)")
    high_conflict_flag: bool = Field(
        False, description="當 K >= 0.99 時觸發安全防除零回退並標記需高階仲裁"
    )
    modality_bpas: list[ModalityBpa] = Field(default_factory=list)


class AntiFalseAlarmFlag(BaseModel):
    """防誤報過濾規則評估旗標。"""

    rule_name: str
    triggered: bool = Field(..., description="True 表示偵測到特定雜訊並成功過濾抑制")
    description: str


class ScenarioEvaluation(BaseModel):
    """可插拔場景模組統一評估結果。"""

    scenario_id: ScenarioId
    scenario_name: str
    risk_level: RiskLevel
    triggered: bool = Field(..., description="是否觸發該場景之目標偵測或安全／節能動作")
    false_alarm_suppressed: bool = Field(False, description="是否成功識別並抑制誤報干擾源")
    trigger_reasons: list[str] = Field(default_factory=list)
    anti_false_alarm_flags: list[AntiFalseAlarmFlag] = Field(default_factory=list)
    northbound_commands: list[dict[str, Any]] = Field(default_factory=list)
    recommended_privacy_level: PrivacyLevel = PrivacyLevel.L0
    confidence: float = Field(0.0, ge=0.0, le=1.0)


class BandwidthMetrics(BaseModel):
    """節點內部匯流排與對外上行頻寬指標。"""

    internal_bus_mbps: float
    uplink_kbps: float
    uplink_mbps: float
    saving_vs_4k_pct: float


class PowerMetrics(BaseModel):
    """模組增量功耗、含 AP 整機功耗與 PoE 預算佔用。"""

    sensor_npu_incremental_w: float
    ap_base_w: float
    total_system_w: float
    poe_budget_w: float
    poe_utilization_pct: float
    poe_warning: bool = False


class LatencyMetrics(BaseModel):
    """端對端分階段處理延遲（ms）。"""

    stage0_rf_ms: float
    stage1_l1_wakeup_ms: float
    stage2_rgb_align_ms: float
    stage3_npu_infer_ms: float
    stage4_encrypt_tx_ms: float
    total_e2e_ms: float
    warm_standby_enabled: bool = True


class SensorActiveStatus(BaseModel):
    """四模態感測器當前取樣率與運行狀態。"""

    csi_hz: float
    mmwave_hz: float
    mmwave_mode: str
    thermal_hz: float
    thermal_mode: str
    rgb_fps: float
    rgb_mode: str


class AuditRecord(BaseModel):
    """隱私清零、授權解密與北向聯動稽核日誌紀錄。"""

    id: str
    timestamp_iso: str
    sim_time_s: float
    event_type: str
    privacy_level: PrivacyLevel
    scenario_id: str
    ram_wipe_ms: Optional[float] = None
    zero_disk_verified: bool = True
    detail: str
    northbound_action: Optional[str] = None


class L3AuthorizeRequest(BaseModel):
    """L3 雙因子緊急授權／解除警報請求荷載。"""

    operator_id: str = Field("sec-op-01", description="值班授權人員或家屬識別 ID")
    otp_token: str = Field("842916", description="6 位數動態雙因子權杖（示範預設 842916）")
    action: str = Field("grant", description="操作類型：grant（授權啟封存證）或 revoke（解除警報復歸）")
    reason: str = Field("緊急人工覆核與現場安全確認", description="稽核事由說明")


class ReplayStatus(BaseModel):
    """Log-Replay 時間軸回放即時狀態。"""

    active_track_id: Optional[str] = None
    track_title: str = ""
    dataset_source: str = ""
    frame_index: int = 0
    total_frames: int = 0
    progress_pct: float = 0.0
    playing: bool = True
    speed: float = 1.0
    loop: bool = True
    gt_segment: str = "N/A"
    gt_is_alert: bool = False
    single_modality_would_false_alarm: bool = False
    single_modality_note: str = ""


class LiveCameraStatus(BaseModel):
    """混合實況（Hybrid Live）真實攝影機與隱私閘門狀態。"""

    enabled: bool = False
    camera_source: str = "none"              # none / browser_getusermedia / local_opencv
    camera_hardware_state: str = "PHYSICALLY_OFF"  # PHYSICALLY_OFF / L2_EPHEMERAL_RAM / L3_AUTHORIZED_STREAM / REJECTED_BY_GATE
    pose_backend: str = "offline_hog_contour"
    last_live_wipe_ms: float = 0.0
    live_person_detected: bool = False
    live_posture_label: str = "none"
    live_frame_seq: int = 0
    rejected_attempts: int = 0
    l3_preview_b64: Optional[str] = None
    note: str = "預設關閉實體鏡頭（Camera-Off by Default）"


class TrackSummary(BaseModel):
    """可回放時間軸資料集摘要描述。"""

    track_id: str
    title: str
    scenario_id: ScenarioId
    dataset_source: str
    description: str
    total_frames: int
    fps_nominal: float = 8.0
    is_uploaded: bool = False


class TrackEvaluationReport(BaseModel):
    """單一或整體回放資料集之高層批次評估報表。"""

    track_id: str
    title: str
    scenario_id: str
    total_frames: int
    tp: int
    fp: int
    tn: int
    fn: int
    accuracy_pct: float
    precision_pct: float
    recall_pct: float
    f1_score: float
    single_modality_false_alarms: int
    four_modality_false_alarms: int
    far_reduction_pct: float
    avg_e2e_latency_ms: float
    avg_ram_wipe_ms: float
    avg_bandwidth_saving_pct: float
    standby_bandwidth_saving_pct: float = 0.0
    b2_occupied_hold_rate_pct: Optional[float] = None


class SystemStateSnapshot(BaseModel):
    """即時推送至戰情室與 REST /api/state 的系統全景快照。"""

    sim_time_s: float
    source_mode: SourceMode = SourceMode.SIMULATOR
    active_event: str
    active_scenario: ScenarioId
    hardware_profile: HardwareProfile
    privacy_level: PrivacyLevel
    privacy_compliance_tier: str
    hysteresis_target_level: Optional[PrivacyLevel] = None
    hysteresis_elapsed_s: float = 0.0
    hysteresis_required_s: float = 35.0
    graceful_degradation_active: bool = False
    npu_load_pct: float = 0.0
    l3_authorized: bool = False
    l3_operator_id: Optional[str] = None
    sensor_status: SensorActiveStatus
    aligned_frame: AlignedFrame
    ds_fusion: DsFusionResult
    evaluation: ScenarioEvaluation
    bandwidth: BandwidthMetrics
    power: PowerMetrics
    latency: LatencyMetrics
    last_ram_wipe_ms: float = 0.0
    zero_disk_persistence: bool = True
    recent_audits: list[AuditRecord] = Field(default_factory=list)
    replay_status: Optional[ReplayStatus] = None
    live_camera_status: Optional[LiveCameraStatus] = None
