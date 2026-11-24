#pragma once

/**
 * @file privacy_fsm.hpp
 * @brief Privacy-by-Design 四階隱私狀態機（L0–L3）、防抖遲滯與 Graceful Degradation 保護
 *
 * 三大硬性工程鐵律實作：
 * 1. Camera-Off by Default：於 L0（常駐 RF）與 L1（非視覺物理驗證）階段，硬體/邏輯雙重拒絕
 *    任何 RawRgbFrame 寫入 TEE Volatile RAM，確保日常 95%+ 時間絕對零光學擷取。
 * 2. Volatile RAM Only + <100ms 強制清零：於 L2 提取 17 點去識別化骨架後立即自動呼叫
 *    SecureRingBuffer::secure_wipe() 並回讀驗證全零（Zero Disk Persistence）。
 * 3. L3 雙重授權/連續確認加密存證 + 遲滯防抖（30–60s Hysteresis）+ NPU 過載降幀保護
 *    （Graceful Degradation：滿載時將 RGB 30fps 降至 10fps，維持 20Hz 雷達與熱像追蹤）。
 */

#include "ds_fusion.hpp"
#include "secure_ring_buffer.hpp"
#include "sensor_types.hpp"
#include "sliding_window.hpp"

#include <cstddef>
#include <cstdint>

namespace edge_fusion {

inline constexpr std::size_t   kTeeRamRingCapacity       = 16U;
inline constexpr std::uint64_t kDefaultHysteresisNs      = 30'000'000'000ULL;  // 30 秒預設防抖冷卻
inline constexpr std::uint32_t kDefaultCriticalConfirmN  = 3U;                 // L2->L3 連續確認幀數

using TeeRawRgbRing = SecureRingBuffer<RawRgbFrame, kTeeRamRingCapacity>;

/// @brief Privacy-by-Design 四階隱私狀態列舉
enum class PrivacyLevel : std::uint8_t {
    L0_AmbientRf         = 0U,  ///< 常駐低功耗 Wi-Fi 802.11bf CSI + BLE/UWB（RGB/Thermal 休眠）
    L1_NonVisualVerify   = 1U,  ///< 喚醒 60GHz mmWave + 低解析 Thermal（無光學影像）
    L2_DeidentifiedRam   = 2U,  ///< TEE Volatile RAM 去識別化 17 點骨架推論（推論後立即清零）
    L3_EncryptedForensics = 3U  ///< 連續危急確認或雙因子授權：加密 Pre-roll 存證與聯動
};

/// @brief 狀態轉移與保護事件原因代碼
enum class FsmTransitionReason : std::uint8_t {
    None = 0U,
    L0ToL1_RfDisturbance,
    L1ToL2_NonVisualAlertOrConflict,
    L2ToL3_CriticalConfirmed,
    L2ToL3_DualCustodyAuthorized,
    Downgrade_HysteresisComplete,
    Hold_HysteresisCooldownActive,
    Guard_RawCameraRejectedInL0L1,
    Degradation_NpuOverloadRgbThrottled,
    Degradation_RecoveredToNormal
};

/// @brief Raw RGB 寫入守衛回傳狀態
enum class RawCaptureGuardStatus : std::uint8_t {
    AcceptedIntoTeeRam = 0U,
    RejectedCameraOffInL0L1,
    SkippedByGracefulDegradation
};

/// @brief 狀態機組態參數
struct PrivacyFsmConfig {
    HardwareTier  hardware_tier{HardwareTier::Tier2AllInOne};
    std::uint64_t hysteresis_ns{kDefaultHysteresisNs};
    std::uint32_t critical_confirm_frames{kDefaultCriticalConfirmN};
    double        l0_to_l1_csi_variance_threshold{0.25};
    double        l1_to_l2_belief_alert_threshold{0.55};
    double        l2_to_l3_belief_alert_threshold{0.82};
    double        downgrade_safe_belief_threshold{0.30};
    float         degradation_recover_threshold_pct{70.0F};
};

/// @brief 狀態機即時資源、功耗、頻寬與隱私稽核快照
struct FsmTelemetrySnapshot {
    PrivacyLevel        level{PrivacyLevel::L0_AmbientRf};
    FsmTransitionReason last_reason{FsmTransitionReason::None};

    bool          camera_capture_allowed{false};
    bool          graceful_degradation_active{false};
    bool          hysteresis_cooldown_active{false};
    double        hysteresis_remaining_sec{0.0};

    std::uint16_t effective_rgb_fps{0U};
    std::uint16_t effective_mmwave_hz{0U};
    std::uint16_t effective_thermal_hz{0U};
    std::uint16_t effective_csi_hz{100U};

    float         estimated_internal_bw_mbps{0.8F};
    float         estimated_uplink_bw_kbps{3.5F};
    float         estimated_incremental_watts{0.6F};
    float         estimated_total_ap_watts{13.1F};
    bool          poe_budget_exceeded{false};

    std::uint8_t  privacy_compliance_tier{0U};
    bool          zero_disk_persistence{true};

    std::uint64_t rejected_raw_capture_count{0ULL};
    std::uint64_t l2_auto_wipe_count{0ULL};
    std::uint64_t wipe_verification_failures{0ULL};
    WipeReport    last_wipe_report{};
};

/// @brief Privacy-by-Design L0–L3 狀態機控制器
class PrivacyFsm {
public:
    explicit PrivacyFsm(const PrivacyFsmConfig& cfg = PrivacyFsmConfig{}) noexcept
        : config_(cfg) {}

    void reset() noexcept;
    void set_config(const PrivacyFsmConfig& cfg) noexcept { config_ = cfg; }
    [[nodiscard]] const PrivacyFsmConfig& config() const noexcept { return config_; }

    [[nodiscard]] PrivacyLevel current_level() const noexcept { return level_; }

    /// @brief 硬性守衛：僅 L2 與 L3 允許將 RawRgbFrame 寫入 TEE Volatile RAM
    [[nodiscard]] bool can_capture_raw_rgb() const noexcept {
        return level_ == PrivacyLevel::L2_DeidentifiedRam ||
               level_ == PrivacyLevel::L3_EncryptedForensics;
    }

    /// @brief 嘗試擷取原始 RGB 影格至 TEE 揮發性環狀緩衝區
    /// @details 於 L0/L1 無條件拒絕並遞增稽核計數；於 Graceful Degradation 降幀週期內自動跳過部分幀
    RawCaptureGuardStatus ingest_raw_rgb_frame(const RawRgbFrame& raw_frame,
                                               TeeRawRgbRing& tee_ram_buffer) noexcept;

    /// @brief L2 推論並立即執行安全清零（從 Volatile RAM 提取去識別化 17 點骨架後強制 secure_wipe）
    /// @return true 代表推論完成且緩衝區已通過全零驗證；false 代表緩衝為空或清零驗證失敗
    bool run_l2_inference_and_wipe(TeeRawRgbRing& tee_ram_buffer,
                                   RgbSkeletonFrame& out_skeleton,
                                   bool simulated_person_present,
                                   bool simulated_fall_posture,
                                   bool simulated_flame_smoke,
                                   bool simulated_vehicle_heat_mask = false) noexcept;

    /// @brief 依據當前對齊切片、D-S 融合結果、授權權杖與 NPU 負載更新狀態機
    /// @param tee_ram_buffer 當從 L2/L3 降階至 L1/L0 時自動觸發全域 secure_wipe()
    FsmTelemetrySnapshot update(std::uint64_t now_ptp_ns,
                                const AlignedMultiModalSlice& slice,
                                const DsFusionResult& ds_result,
                                bool dual_custody_authorized,
                                float npu_load_pct,
                                float memory_bw_load_pct,
                                TeeRawRgbRing& tee_ram_buffer) noexcept;

    [[nodiscard]] FsmTelemetrySnapshot snapshot(std::uint64_t now_ptp_ns = 0ULL) const noexcept;

private:
    void update_degradation_state(float npu_load_pct, float memory_bw_load_pct) noexcept;
    void handle_downgrade_wipe(PrivacyLevel old_level,
                               PrivacyLevel new_level,
                               TeeRawRgbRing& tee_ram_buffer) noexcept;

    PrivacyFsmConfig    config_{};
    PrivacyLevel        level_{PrivacyLevel::L0_AmbientRf};
    FsmTransitionReason last_reason_{FsmTransitionReason::None};

    bool          graceful_degradation_active_{false};
    bool          cooldown_active_{false};
    std::uint64_t cooldown_start_ptp_ns_{0ULL};
    std::uint64_t last_now_ptp_ns_{0ULL};
    std::uint32_t consecutive_critical_frames_{0U};
    std::uint64_t rgb_ingest_counter_{0ULL};

    std::uint64_t rejected_raw_capture_count_{0ULL};
    std::uint64_t l2_auto_wipe_count_{0ULL};
    std::uint64_t wipe_verification_failures_{0ULL};
    WipeReport    last_wipe_report_{};
};

}  // namespace edge_fusion
