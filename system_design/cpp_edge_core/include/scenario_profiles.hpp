#pragma once

/**
 * @file scenario_profiles.hpp
 * @brief 五大旗艦應用場景（A1, C4, B1, C1, B2）多型策略介面與防誤報濾波規則
 *
 * 透過純虛擬介面 IScenarioProfile 與靜態固定實例（無動態配置），為五大場景提供專屬：
 * 1. 四模態 D-S 證據生成與可靠度權重（Reliability α_i）。
 * 2. 場景告警閾值與北向聯動動作（MQTT 護理鈴 / BMS 急停 / VMS / 安全 PLC / BACnet）。
 * 3. 領域物理防誤報抑制規則（例如寵物與水蒸氣過濾、車輛煞車餘熱過濾、巡檢白名單、
 *    水霧與晃動電纜過濾、玻璃外行人排除等）。
 */

#include "ds_fusion.hpp"
#include "privacy_fsm.hpp"
#include "sensor_types.hpp"
#include "sliding_window.hpp"

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>

namespace edge_fusion {

/// @brief 場景風險嚴重程度分級
enum class AlertSeverity : std::uint8_t {
    Safe     = 0U,
    Advisory = 1U,
    Warning  = 2U,
    Critical = 3U
};

/// @brief 防誤報過濾原因代碼
enum class SuppressionReason : std::uint8_t {
    None = 0U,
    PetOrColdObjectFiltered,         ///< A1：非人類低溫物體（棉被/抱枕）或寵物活動
    WaterVaporFiltered,              ///< A1：浴室高濕水蒸氣無跌倒落差與人體輪廓
    VehicleExhaustOrSunGlintFiltered,///< C4：車輛煞車碟盤/引擎蓋餘熱或日照反光（非電池熱失控）
    AuthorizedWhitelistMatched,      ///< B1/C1：UWB/BLE AoA 授權巡檢員或合格作業員白名單
    SwayingTreeOrAnimalFiltered,     ///< B1：搖晃樹影或低矮四足溫血動物
    WaterMistOrCableSwingFiltered,   ///< C1：冷鏈水霧或無生命低溫垂掛電纜晃動
    GlassOutsidePasserbyFiltered,    ///< B2：雷達穿透玻璃牆誤測走廊行人（LWIR 被玻璃阻隔證實無室內體熱）
    StaticHeatSourceFiltered         ///< B2：無呼吸微動之靜態非人發熱源（暖氣/伺服器）
};

/// @brief 北向自動化聯動執行指令
enum class ActuatorCommand : std::uint8_t {
    None = 0U,
    NurseCallAlert,      ///< A1：觸發護理站 MQTT/SIP 緊急呼救與語音關懷
    BmsEmergencyTrip,    ///< C4：觸發 BESS BMS / 充電樁 Modbus TCP 斷電急停跳脫
    SecuritySirenAndVms, ///< B1：觸發園區電子圍籬告警與 VMS 加密存證
    PlcSafeSlowdown,     ///< C1：ISO/TS 15066 動態安全降速（Profinet/Modbus）
    PlcEmergencyStop,    ///< C1：緊急停止機械臂/堆高機（E-Stop）
    HvacComfortHold,     ///< B2：確認人員靜坐呼吸存在，維持舒適空調（消除 False-Off）
    HvacEcoSetback       ///< B2：確認無人（或僅玻璃外路人），5-10 分鐘切換節能模式
};

/// @brief 場景權重與決策閾值固定配置
struct ScenarioConfig {
    ScenarioId       id;
    std::string_view code;
    std::string_view name_zh;

    double weight_csi;
    double weight_mmwave;
    double weight_thermal;
    double weight_rgb;

    double warning_belief_threshold;
    double critical_belief_threshold;
};

/// @brief 單幀場景策略評估輸出（零動態配置）
struct ScenarioEvaluation {
    ScenarioId        id{ScenarioId::A1_ElderlyCare};
    std::string_view  scenario_code{"A1"};
    AlertSeverity     severity{AlertSeverity::Safe};
    bool              alert_triggered{false};
    bool              false_alarm_suppressed{false};
    SuppressionReason suppression_reason{SuppressionReason::None};
    ActuatorCommand   actuator_action{ActuatorCommand::None};
    double            confidence_score{0.0};
    double            safety_distance_m{99.0};
    std::string_view  status_summary_zh{"正常監測中"};
};

/// @brief 五大場景統一多型策略介面
class IScenarioProfile {
public:
    virtual ~IScenarioProfile() = default;

    [[nodiscard]] virtual ScenarioId id() const noexcept = 0;
    [[nodiscard]] virtual std::string_view code() const noexcept = 0;
    [[nodiscard]] virtual std::string_view name_zh() const noexcept = 0;
    [[nodiscard]] virtual const ScenarioConfig& config() const noexcept = 0;

    /// @brief 將對齊後的多模態切片轉為各模態基本機率分配（BPA）與可靠度權重
    [[nodiscard]] virtual std::array<ModalityEvidence, kMaxDsSources> build_evidence(
        const AlignedMultiModalSlice& slice) const noexcept = 0;

    /// @brief 結合 D-S 融合結果與領域物理規則產出最終防誤報判定與聯動指令
    [[nodiscard]] virtual ScenarioEvaluation evaluate(
        const AlignedMultiModalSlice& slice,
        const DsFusionResult& ds_result,
        PrivacyLevel current_level) const noexcept = 0;
};

/// @brief A1：銀髮居家與長照衛浴／臥室零穿戴跌倒與生命徵象守護
class ScenarioA1ElderlyCare final : public IScenarioProfile {
public:
    [[nodiscard]] ScenarioId id() const noexcept override { return ScenarioId::A1_ElderlyCare; }
    [[nodiscard]] std::string_view code() const noexcept override { return "A1"; }
    [[nodiscard]] std::string_view name_zh() const noexcept override {
        return "銀髮居家與長照衛浴零穿戴跌倒守護";
    }
    [[nodiscard]] const ScenarioConfig& config() const noexcept override;
    [[nodiscard]] std::array<ModalityEvidence, kMaxDsSources> build_evidence(
        const AlignedMultiModalSlice& slice) const noexcept override;
    [[nodiscard]] ScenarioEvaluation evaluate(
        const AlignedMultiModalSlice& slice,
        const DsFusionResult& ds_result,
        PrivacyLevel current_level) const noexcept override;
};

/// @brief C4：BESS 儲能站／EV 超充站熱失控 Stage 1 截斷與濃煙搜救
class ScenarioC4BessThermal final : public IScenarioProfile {
public:
    [[nodiscard]] ScenarioId id() const noexcept override { return ScenarioId::C4_BessThermal; }
    [[nodiscard]] std::string_view code() const noexcept override { return "C4"; }
    [[nodiscard]] std::string_view name_zh() const noexcept override {
        return "BESS 儲能站熱失控 Stage 1 截斷與濃煙搜救";
    }
    [[nodiscard]] const ScenarioConfig& config() const noexcept override;
    [[nodiscard]] std::array<ModalityEvidence, kMaxDsSources> build_evidence(
        const AlignedMultiModalSlice& slice) const noexcept override;
    [[nodiscard]] ScenarioEvaluation evaluate(
        const AlignedMultiModalSlice& slice,
        const DsFusionResult& ds_result,
        PrivacyLevel current_level) const noexcept override;
};

/// @brief B1：關鍵園區／倉儲全天候零誤報周界入侵偵測
class ScenarioB1Perimeter final : public IScenarioProfile {
public:
    [[nodiscard]] ScenarioId id() const noexcept override { return ScenarioId::B1_Perimeter; }
    [[nodiscard]] std::string_view code() const noexcept override { return "B1"; }
    [[nodiscard]] std::string_view name_zh() const noexcept override {
        return "關鍵園區全天候零誤報周界入侵偵測";
    }
    [[nodiscard]] const ScenarioConfig& config() const noexcept override;
    [[nodiscard]] std::array<ModalityEvidence, kMaxDsSources> build_evidence(
        const AlignedMultiModalSlice& slice) const noexcept override;
    [[nodiscard]] ScenarioEvaluation evaluate(
        const AlignedMultiModalSlice& slice,
        const DsFusionResult& ds_result,
        PrivacyLevel current_level) const noexcept override;
};

/// @brief C1：智慧工廠人機協作（HRC）／粉塵冷鏈盲區動態防撞
class ScenarioC1HrcSafety final : public IScenarioProfile {
public:
    [[nodiscard]] ScenarioId id() const noexcept override { return ScenarioId::C1_HrcSafety; }
    [[nodiscard]] std::string_view code() const noexcept override { return "C1"; }
    [[nodiscard]] std::string_view name_zh() const noexcept override {
        return "人機協作 (HRC) 與粉塵盲區動態安全防護";
    }
    [[nodiscard]] const ScenarioConfig& config() const noexcept override;
    [[nodiscard]] std::array<ModalityEvidence, kMaxDsSources> build_evidence(
        const AlignedMultiModalSlice& slice) const noexcept override;
    [[nodiscard]] ScenarioEvaluation evaluate(
        const AlignedMultiModalSlice& slice,
        const DsFusionResult& ds_result,
        PrivacyLevel current_level) const noexcept override;
};

/// @brief B2：智慧商辦微氣候 HVAC 零 False-Off 節能與夜間分級安防
class ScenarioB2SmartHvac final : public IScenarioProfile {
public:
    [[nodiscard]] ScenarioId id() const noexcept override { return ScenarioId::B2_SmartHvac; }
    [[nodiscard]] std::string_view code() const noexcept override { return "B2"; }
    [[nodiscard]] std::string_view name_zh() const noexcept override {
        return "智慧商辦 HVAC 零 False-Off 節能與安防";
    }
    [[nodiscard]] const ScenarioConfig& config() const noexcept override;
    [[nodiscard]] std::array<ModalityEvidence, kMaxDsSources> build_evidence(
        const AlignedMultiModalSlice& slice) const noexcept override;
    [[nodiscard]] ScenarioEvaluation evaluate(
        const AlignedMultiModalSlice& slice,
        const DsFusionResult& ds_result,
        PrivacyLevel current_level) const noexcept override;
};

/// @brief 取得五大場景之無配置靜態多型策略指標
[[nodiscard]] const IScenarioProfile& get_scenario_profile(ScenarioId id) noexcept;

/// @brief 完整整合邊緣融合核心（串接對齊器 + D-S 引擎 + L0–L3 狀態機 + 場景策略，零動態配置）
struct CoreStepInput {
    std::uint64_t target_ptp_ns{0ULL};
    ScenarioId    scenario{ScenarioId::A1_ElderlyCare};
    bool          dual_custody_authorized{false};
    float         npu_load_pct{20.0F};
    float         memory_bw_load_pct{20.0F};
};

struct CoreStepOutput {
    AlignedMultiModalSlice aligned_slice{};
    DsFusionResult         ds_result{};
    FsmTelemetrySnapshot   fsm_snapshot{};
    ScenarioEvaluation     evaluation{};
    std::uint64_t          processing_latency_ns{0ULL};
};

class EdgeFusionCore {
public:
    explicit EdgeFusionCore(const PrivacyFsmConfig& fsm_cfg = PrivacyFsmConfig{}) noexcept
        : fsm_(fsm_cfg) {}

    void reset() noexcept;

    bool push_csi(const CsiFrame& frame) noexcept { return aligner_.push_csi(frame); }
    bool push_mmwave(const MmWaveFrame& frame) noexcept { return aligner_.push_mmwave(frame); }
    bool push_thermal(const ThermalFrame& frame) noexcept { return aligner_.push_thermal(frame); }
    bool push_rgb_skeleton(const RgbSkeletonFrame& frame) noexcept {
        return aligner_.push_rgb_skeleton(frame);
    }

    RawCaptureGuardStatus ingest_raw_rgb(const RawRgbFrame& raw_frame) noexcept {
        return fsm_.ingest_raw_rgb_frame(raw_frame, tee_raw_ring_);
    }

    bool execute_l2_skeleton_and_wipe(RgbSkeletonFrame& out_skeleton,
                                      bool person_present,
                                      bool fall_posture,
                                      bool flame_smoke,
                                      bool vehicle_heat_mask = false) noexcept;

    /// @brief 單步完整熱路徑執行（全過程零 heap allocation）
    [[nodiscard]] CoreStepOutput step(const CoreStepInput& input) noexcept;

    [[nodiscard]] const PrivacyFsm& fsm() const noexcept { return fsm_; }
    [[nodiscard]] PrivacyFsm& fsm() noexcept { return fsm_; }
    [[nodiscard]] const SlidingWindowAligner& aligner() const noexcept { return aligner_; }
    [[nodiscard]] const TeeRawRgbRing& tee_raw_ring() const noexcept { return tee_raw_ring_; }
    [[nodiscard]] TeeRawRgbRing& tee_raw_ring() noexcept { return tee_raw_ring_; }

private:
    SlidingWindowAligner aligner_{};
    PrivacyFsm           fsm_{};
    TeeRawRgbRing        tee_raw_ring_{};
};

}  // namespace edge_fusion
