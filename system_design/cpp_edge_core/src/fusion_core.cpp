/**
 * @file fusion_core.cpp
 * @brief 四模態嵌入式融合核心實作（對齊器、D-S 融合、L0–L3 狀態機、五大場景與 EdgeFusionCore）
 *
 * 嚴格遵守關鍵熱路徑零動態記憶體配置（Zero Heap Allocation），
 * 全程僅操作固定大小緩衝區、堆疊變數與靜態唯讀配置表。
 */

#include "ds_fusion.hpp"
#include "privacy_fsm.hpp"
#include "scenario_profiles.hpp"
#include "secure_ring_buffer.hpp"
#include "sensor_types.hpp"
#include "sliding_window.hpp"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstddef>
#include <cstdint>

namespace edge_fusion {

namespace {

[[nodiscard]] constexpr double clamp01(double v) noexcept {
    if (v < 0.0) {
        return 0.0;
    }
    if (v > 1.0) {
        return 1.0;
    }
    return v;
}

[[nodiscard]] float clamp01_f(float v) noexcept {
    if (v < 0.0F) {
        return 0.0F;
    }
    if (v > 1.0F) {
        return 1.0F;
    }
    return v;
}

[[nodiscard]] std::uint64_t abs_diff_u64(std::uint64_t a, std::uint64_t b) noexcept {
    return (a >= b) ? (a - b) : (b - a);
}

template <typename QueueT>
std::uint64_t count_outside_window(const QueueT& q,
                                   std::uint64_t target_ptp_ns,
                                   std::uint64_t window_ns) noexcept {
    std::uint64_t rejected = 0ULL;
    for (std::size_t i = 0U; i < q.size(); ++i) {
        if (abs_diff_u64(target_ptp_ns, q.at(i).ptp_timestamp_ns) > window_ns) {
            ++rejected;
        }
    }
    return rejected;
}

void extrapolate_mmwave_first_order(MmWaveFrame& frame, double dt_sec) noexcept {
    if (dt_sec <= 0.0) {
        return;
    }

    const double v2 = static_cast<double>(frame.vx_mps) * static_cast<double>(frame.vx_mps) +
                      static_cast<double>(frame.vy_mps) * static_cast<double>(frame.vy_mps) +
                      static_cast<double>(frame.vz_mps) * static_cast<double>(frame.vz_mps);

    if (v2 > 1.0e-8) {
        // 使用完整 3D 速度向量執行一階線性位置外推：p(t) = p(t_r) + v * dt
        frame.target_x_m = static_cast<float>(static_cast<double>(frame.target_x_m) +
                                              static_cast<double>(frame.vx_mps) * dt_sec);
        frame.target_y_m = static_cast<float>(static_cast<double>(frame.target_y_m) +
                                              static_cast<double>(frame.vy_mps) * dt_sec);
        frame.target_z_m = static_cast<float>(static_cast<double>(frame.target_z_m) +
                                              static_cast<double>(frame.vz_mps) * dt_sec);
    } else if (std::abs(frame.radial_velocity_mps) > 1.0e-5F) {
        // 若僅有徑向都卜勒速度，沿徑向單位向量執行一階位置外推
        const double r = std::sqrt(
            static_cast<double>(frame.target_x_m) * static_cast<double>(frame.target_x_m) +
            static_cast<double>(frame.target_y_m) * static_cast<double>(frame.target_y_m) +
            static_cast<double>(frame.target_z_m) * static_cast<double>(frame.target_z_m));
        if (r > 1.0e-4) {
            const double dr = static_cast<double>(frame.radial_velocity_mps) * dt_sec;
            frame.target_x_m = static_cast<float>(
                static_cast<double>(frame.target_x_m) +
                dr * (static_cast<double>(frame.target_x_m) / r));
            frame.target_y_m = static_cast<float>(
                static_cast<double>(frame.target_y_m) +
                dr * (static_cast<double>(frame.target_y_m) / r));
            frame.target_z_m = static_cast<float>(
                static_cast<double>(frame.target_z_m) +
                dr * (static_cast<double>(frame.target_z_m) / r));
        }
    }

    // 同步對有效點雲執行一階徑向外推補償
    const std::size_t n_pts = std::min<std::size_t>(frame.point_count, kMaxMmWavePoints);
    for (std::size_t i = 0U; i < n_pts; ++i) {
        MmWavePoint& pt = frame.points[i];
        const double r  = std::sqrt(static_cast<double>(pt.x_m) * static_cast<double>(pt.x_m) +
                                   static_cast<double>(pt.y_m) * static_cast<double>(pt.y_m) +
                                   static_cast<double>(pt.z_m) * static_cast<double>(pt.z_m));
        if (r > 1.0e-4 && std::abs(pt.radial_velocity_mps) > 1.0e-5F) {
            const double dr = static_cast<double>(pt.radial_velocity_mps) * dt_sec;
            pt.x_m = static_cast<float>(static_cast<double>(pt.x_m) +
                                        dr * (static_cast<double>(pt.x_m) / r));
            pt.y_m = static_cast<float>(static_cast<double>(pt.y_m) +
                                        dr * (static_cast<double>(pt.y_m) / r));
            pt.z_m = static_cast<float>(static_cast<double>(pt.z_m) +
                                        dr * (static_cast<double>(pt.z_m) / r));
        }
    }
}

[[nodiscard]] double compute_radar_distance_m(const MmWaveFrame& mm) noexcept {
    return std::sqrt(static_cast<double>(mm.target_x_m) * static_cast<double>(mm.target_x_m) +
                     static_cast<double>(mm.target_y_m) * static_cast<double>(mm.target_y_m) +
                     static_cast<double>(mm.target_z_m) * static_cast<double>(mm.target_z_m));
}

}  // namespace

// ============================================================================
// 1. SlidingWindowAligner 實作
// ============================================================================

void SlidingWindowAligner::reset() noexcept {
    latest_seen_ptp_ns_ = 0ULL;
    csi_queue_.clear();
    mmwave_queue_.clear();
    thermal_queue_.clear();
    rgb_queue_.clear();
}

void SlidingWindowAligner::track_latest_ptp(std::uint64_t ptp_ns) noexcept {
    if (ptp_ns > latest_seen_ptp_ns_) {
        latest_seen_ptp_ns_ = ptp_ns;
    }
}

bool SlidingWindowAligner::push_csi(const CsiFrame& frame) noexcept {
    track_latest_ptp(frame.ptp_timestamp_ns);
    return csi_queue_.insert(frame);
}

bool SlidingWindowAligner::push_mmwave(const MmWaveFrame& frame) noexcept {
    track_latest_ptp(frame.ptp_timestamp_ns);
    return mmwave_queue_.insert(frame);
}

bool SlidingWindowAligner::push_thermal(const ThermalFrame& frame) noexcept {
    track_latest_ptp(frame.ptp_timestamp_ns);
    return thermal_queue_.insert(frame);
}

bool SlidingWindowAligner::push_rgb_skeleton(const RgbSkeletonFrame& frame) noexcept {
    track_latest_ptp(frame.ptp_timestamp_ns);
    return rgb_queue_.insert(frame);
}

std::size_t SlidingWindowAligner::prune_expired(std::uint64_t anchor_ptp_ns) noexcept {
    if (anchor_ptp_ns <= window_ns_) {
        return 0U;
    }
    const std::uint64_t cutoff = anchor_ptp_ns - window_ns_;
    std::size_t total_removed  = 0U;
    total_removed += csi_queue_.prune_older_than(cutoff);
    total_removed += mmwave_queue_.prune_older_than(cutoff);
    total_removed += thermal_queue_.prune_older_than(cutoff);
    total_removed += rgb_queue_.prune_older_than(cutoff);
    return total_removed;
}

std::uint64_t SlidingWindowAligner::total_dropped_stale() const noexcept {
    return csi_queue_.dropped_stale_count() + mmwave_queue_.dropped_stale_count() +
           thermal_queue_.dropped_stale_count() + rgb_queue_.dropped_stale_count();
}

AlignedMultiModalSlice SlidingWindowAligner::align(std::uint64_t target_ptp_ns) const noexcept {
    AlignedMultiModalSlice slice{};
    slice.target_ptp_ns = target_ptp_ns;

    slice.out_of_window_rejected =
        count_outside_window(csi_queue_, target_ptp_ns, window_ns_) +
        count_outside_window(mmwave_queue_, target_ptp_ns, window_ns_) +
        count_outside_window(thermal_queue_, target_ptp_ns, window_ns_) +
        count_outside_window(rgb_queue_, target_ptp_ns, window_ns_);

    // 1. 對齊 Wi-Fi CSI（支援 ±25ms 子視窗內高頻多幀特徵壓縮聚合）
    if (const CsiFrame* best_csi =
            csi_queue_.find_closest(target_ptp_ns, window_ns_, slice.csi_skew_ns);
        best_csi != nullptr) {
        slice.has_csi = true;
        slice.csi     = *best_csi;

        double       sum_var     = 0.0;
        double       sum_energy  = 0.0;
        std::size_t  agg_count   = 0U;
        bool         any_tag     = best_csi->whitelist_tag_present;
        std::uint8_t max_auth    = best_csi->whitelist_auth_level;
        float        min_uwb_m   = best_csi->whitelist_distance_m;

        for (std::size_t i = 0U; i < csi_queue_.size(); ++i) {
            const CsiFrame& item = csi_queue_.at(i);
            if (abs_diff_u64(target_ptp_ns, item.ptp_timestamp_ns) <= kCsiSubWindowNs) {
                sum_var += static_cast<double>(item.fresnel_variance);
                sum_energy += static_cast<double>(item.macro_motion_energy);
                ++agg_count;
                if (item.whitelist_tag_present) {
                    any_tag   = true;
                    max_auth  = std::max(max_auth, item.whitelist_auth_level);
                    min_uwb_m = std::min(min_uwb_m, item.whitelist_distance_m);
                }
            }
        }
        if (agg_count > 0U) {
            slice.csi.fresnel_variance =
                static_cast<float>(sum_var / static_cast<double>(agg_count));
            slice.csi.macro_motion_energy =
                static_cast<float>(sum_energy / static_cast<double>(agg_count));
            slice.csi.whitelist_tag_present = any_tag;
            slice.csi.whitelist_auth_level  = max_auth;
            slice.csi.whitelist_distance_m  = min_uwb_m;
            slice.csi_samples_aggregated    = agg_count;
        } else {
            slice.csi_samples_aggregated = 1U;
        }
    }

    // 2. 對齊 mmWave Radar 並於滯後時執行一階 3D/徑向速度位置外推補償
    if (const MmWaveFrame* best_mm =
            mmwave_queue_.find_closest(target_ptp_ns, window_ns_, slice.mmwave_skew_ns);
        best_mm != nullptr) {
        slice.has_mmwave = true;
        slice.mmwave     = *best_mm;
        if (slice.mmwave_skew_ns > 0LL) {
            const double dt_sec = static_cast<double>(slice.mmwave_skew_ns) * 1.0e-9;
            extrapolate_mmwave_first_order(slice.mmwave, dt_sec);
            slice.mmwave.ptp_timestamp_ns     = target_ptp_ns;
            slice.mmwave_extrapolated         = true;
            slice.mmwave_extrapolation_dt_ms  = dt_sec * 1000.0;
        }
    }

    // 3. 對齊 Thermal 幀
    if (const ThermalFrame* best_th =
            thermal_queue_.find_closest(target_ptp_ns, window_ns_, slice.thermal_skew_ns);
        best_th != nullptr) {
        slice.has_thermal = true;
        slice.thermal     = *best_th;
    }

    // 4. 對齊 L2 去識別化 RGB 骨架幀
    if (const RgbSkeletonFrame* best_rgb =
            rgb_queue_.find_closest(target_ptp_ns, window_ns_, slice.rgb_skew_ns);
        best_rgb != nullptr) {
        slice.has_rgb_skeleton = true;
        slice.rgb_skeleton     = *best_rgb;
    }

    // 檢查所有已出現模態是否皆落於 ≤15 ms 嚴格對齊誤差內
    bool strict_ok = slice.has_csi || slice.has_mmwave || slice.has_thermal || slice.has_rgb_skeleton;
    auto check_skew = [&](bool present, std::int64_t skew) {
        if (present) {
            const std::uint64_t mag = static_cast<std::uint64_t>(skew >= 0 ? skew : -skew);
            if (mag > kStrictSyncToleranceNs) {
                strict_ok = false;
            }
        }
    };
    check_skew(slice.has_csi, slice.csi_skew_ns);
    check_skew(slice.has_mmwave, slice.mmwave_skew_ns);
    check_skew(slice.has_thermal, slice.thermal_skew_ns);
    check_skew(slice.has_rgb_skeleton, slice.rgb_skew_ns);
    slice.within_15ms_strict_sync = strict_ok;

    return slice;
}

// ============================================================================
// 2. DempsterShaferEngine 實作
// ============================================================================

void MassFunction::sanitize_and_normalize() noexcept {
    normal      = (normal > 0.0 && std::isfinite(normal)) ? normal : 0.0;
    alert       = (alert > 0.0 && std::isfinite(alert)) ? alert : 0.0;
    uncertainty = (uncertainty > 0.0 && std::isfinite(uncertainty)) ? uncertainty : 0.0;

    const double total = normal + alert + uncertainty;
    if (total <= 1.0e-12) {
        normal      = 0.0;
        alert       = 0.0;
        uncertainty = 1.0;
        return;
    }
    normal /= total;
    alert /= total;
    uncertainty /= total;
}

MassFunction DempsterShaferEngine::discount_bpa(const MassFunction& bpa,
                                                double reliability_alpha) noexcept {
    MassFunction clean = bpa;
    clean.sanitize_and_normalize();

    const double alpha = clamp01(reliability_alpha);
    MassFunction out{};
    out.normal      = alpha * clean.normal;
    out.alert       = alpha * clean.alert;
    out.uncertainty = (1.0 - alpha) + alpha * clean.uncertainty;
    out.sanitize_and_normalize();
    return out;
}

MassFunction DempsterShaferEngine::combine_pair(const MassFunction& m1,
                                                const MassFunction& m2,
                                                double& out_conflict_k,
                                                bool& out_high_conflict) noexcept {
    MassFunction a = m1;
    MassFunction b = m2;
    a.sanitize_and_normalize();
    b.sanitize_and_normalize();

    // 計算衝突質量 K = m1(N)*m2(A) + m1(A)*m2(N)
    double k = a.normal * b.alert + a.alert * b.normal;
    k        = clamp01(k);
    out_conflict_k = k;

    const double raw_normal =
        a.normal * b.normal + a.normal * b.uncertainty + a.uncertainty * b.normal;
    const double raw_alert =
        a.alert * b.alert + a.alert * b.uncertainty + a.uncertainty * b.alert;
    const double raw_uncertainty = a.uncertainty * b.uncertainty;

    MassFunction fused{};
    if (k >= kHighConflictThreshold) {
        // K >= 0.99 高衝突安全回退（Yager 修正法則）：絕不除以 (1 - K) 避免除零或數值爆炸，
        // 將衝突質量 K 轉移至不確定集合 Θ，同時標記須升階至更高級別進行仲裁。
        out_high_conflict = true;
        fused.normal      = raw_normal;
        fused.alert       = raw_alert;
        fused.uncertainty = raw_uncertainty + k;
        fused.sanitize_and_normalize();
        return fused;
    }

    out_high_conflict = false;
    const double inv_one_minus_k = 1.0 / (1.0 - k);
    fused.normal                 = raw_normal * inv_one_minus_k;
    fused.alert                  = raw_alert * inv_one_minus_k;
    fused.uncertainty            = raw_uncertainty * inv_one_minus_k;
    fused.sanitize_and_normalize();
    return fused;
}

DsFusionResult DempsterShaferEngine::combine_all(
    const std::array<ModalityEvidence, kMaxDsSources>& inputs) noexcept {
    DsFusionResult result{};
    result.evaluated_sources = inputs;

    MassFunction accumulator{0.0, 0.0, 1.0};  // 初始完全無知單位元素 m(Θ) = 1.0
    double       max_k            = 0.0;
    double       one_minus_prod   = 1.0;
    bool         any_high_conflict = false;
    std::size_t  active_count     = 0U;

    for (std::size_t i = 0U; i < kMaxDsSources; ++i) {
        ModalityEvidence& src = result.evaluated_sources[i];
        src.raw_bpa.sanitize_and_normalize();

        if (!src.present || src.reliability_alpha <= 0.0) {
            src.discounted_bpa = MassFunction{0.0, 0.0, 1.0};
            continue;
        }

        src.discounted_bpa = discount_bpa(src.raw_bpa, src.reliability_alpha);
        ++active_count;

        double step_k    = 0.0;
        bool   step_high = false;
        accumulator      = combine_pair(accumulator, src.discounted_bpa, step_k, step_high);

        max_k = std::max(max_k, step_k);
        one_minus_prod *= (1.0 - clamp01(step_k));
        if (step_high) {
            any_high_conflict = true;
        }
    }

    result.fused_mass               = accumulator;
    result.belief_alert             = accumulator.alert;
    result.plausibility_alert       = clamp01(accumulator.alert + accumulator.uncertainty);
    result.belief_normal            = accumulator.normal;
    result.plausibility_normal      = clamp01(accumulator.normal + accumulator.uncertainty);
    result.conflict_k               = max_k;
    result.cumulative_conflict      = clamp01(1.0 - one_minus_prod);
    result.high_conflict_escalation = any_high_conflict;
    result.active_source_count      = active_count;
    return result;
}

// ============================================================================
// 3. PrivacyFsm 實作
// ============================================================================

void PrivacyFsm::reset() noexcept {
    level_                        = PrivacyLevel::L0_AmbientRf;
    last_reason_                  = FsmTransitionReason::None;
    graceful_degradation_active_  = false;
    cooldown_active_              = false;
    cooldown_start_ptp_ns_        = 0ULL;
    last_now_ptp_ns_              = 0ULL;
    consecutive_critical_frames_  = 0U;
    rgb_ingest_counter_           = 0ULL;
    rejected_raw_capture_count_   = 0ULL;
    l2_auto_wipe_count_           = 0ULL;
    wipe_verification_failures_   = 0ULL;
    last_wipe_report_             = WipeReport{};
}

void PrivacyFsm::update_degradation_state(float npu_load_pct, float memory_bw_load_pct) noexcept {
    const HardwareSpec& spec = get_hardware_spec(config_.hardware_tier);
    const float overload_th  = spec.degradation_npu_threshold_pct;
    const float recover_th   = config_.degradation_recover_threshold_pct;

    if (!graceful_degradation_active_) {
        if (npu_load_pct >= overload_th || memory_bw_load_pct >= overload_th) {
            graceful_degradation_active_ = true;
            last_reason_                 = FsmTransitionReason::Degradation_NpuOverloadRgbThrottled;
        }
    } else {
        if (npu_load_pct <= recover_th && memory_bw_load_pct <= recover_th) {
            graceful_degradation_active_ = false;
            last_reason_                 = FsmTransitionReason::Degradation_RecoveredToNormal;
        }
    }
}

void PrivacyFsm::handle_downgrade_wipe(PrivacyLevel old_level,
                                       PrivacyLevel new_level,
                                       TeeRawRgbRing& tee_ram_buffer) noexcept {
    const bool was_camera_tier =
        (old_level == PrivacyLevel::L2_DeidentifiedRam ||
         old_level == PrivacyLevel::L3_EncryptedForensics);
    if (was_camera_tier && new_level < old_level) {
        const WipeReport report = tee_ram_buffer.secure_wipe();
        last_wipe_report_       = report;
        if (!report.verified_all_zero) {
            ++wipe_verification_failures_;
        }
    }
}

RawCaptureGuardStatus PrivacyFsm::ingest_raw_rgb_frame(const RawRgbFrame& raw_frame,
                                                       TeeRawRgbRing& tee_ram_buffer) noexcept {
    // 鐵律 1：L0 / L1 預設關閉攝影機，嚴格拒絕任何 Raw RGB 影格寫入 RAM
    if (!can_capture_raw_rgb()) {
        ++rejected_raw_capture_count_;
        last_reason_ = FsmTransitionReason::Guard_RawCameraRejectedInL0L1;
        return RawCaptureGuardStatus::RejectedCameraOffInL0L1;
    }

    ++rgb_ingest_counter_;

    // Graceful Degradation：NPU / 記憶體頻寬超標時將 RGB 30fps 降採樣至 10fps（每 3 幀保留 1 幀）
    if (graceful_degradation_active_ && level_ == PrivacyLevel::L2_DeidentifiedRam) {
        if (((rgb_ingest_counter_ - 1ULL) % 3ULL) != 0ULL) {
            return RawCaptureGuardStatus::SkippedByGracefulDegradation;
        }
    }

    (void)tee_ram_buffer.push(raw_frame);
    return RawCaptureGuardStatus::AcceptedIntoTeeRam;
}

bool PrivacyFsm::run_l2_inference_and_wipe(TeeRawRgbRing& tee_ram_buffer,
                                             RgbSkeletonFrame& out_skeleton,
                                             bool simulated_person_present,
                                             bool simulated_fall_posture,
                                             bool simulated_flame_smoke,
                                             bool simulated_vehicle_heat_mask) noexcept {
    const RawRgbFrame* latest_raw = tee_ram_buffer.peek_latest(0U);
    if (latest_raw == nullptr || !latest_raw->valid) {
        return false;
    }

    // 模擬透過 DMA-BUF 描述子零拷貝讀取 TEE RAM 執行去識別化骨架推論
    const DmaBufDescriptor dma_desc = tee_ram_buffer.export_dma_buf(0U);
    if (!dma_desc.valid) {
        return false;
    }

    // 僅提取去識別化中繼資料（17 點骨架與語意旗標），絕不複製任何像素陣列
    out_skeleton                          = RgbSkeletonFrame{};
    out_skeleton.ptp_timestamp_ns         = latest_raw->ptp_timestamp_ns;
    out_skeleton.monotonic_rx_ns          = latest_raw->monotonic_rx_ns;
    out_skeleton.sequence_id              = latest_raw->sequence_id;
    out_skeleton.valid                    = true;
    out_skeleton.person_detected          = simulated_person_present;
    out_skeleton.posture_fall_detected    = simulated_fall_posture;
    out_skeleton.flame_or_smoke_detected  = simulated_flame_smoke;
    out_skeleton.vehicle_heat_source_mask = simulated_vehicle_heat_mask;
    out_skeleton.ppe_helmet_ok            = true;
    out_skeleton.signal_quality           = 0.95F;

    if (simulated_person_present) {
        out_skeleton.bbox_x_norm = 0.35F;
        out_skeleton.bbox_y_norm = simulated_fall_posture ? 0.62F : 0.18F;
        out_skeleton.bbox_w_norm = simulated_fall_posture ? 0.42F : 0.22F;
        out_skeleton.bbox_h_norm = simulated_fall_posture ? 0.20F : 0.68F;
        out_skeleton.semantic_alert_confidence =
            simulated_fall_posture ? 0.92F : (simulated_flame_smoke ? 0.90F : 0.35F);

        for (std::size_t k = 0U; k < kNumSkeletonKeypoints; ++k) {
            const float ratio = static_cast<float>(k) / static_cast<float>(kNumSkeletonKeypoints);
            out_skeleton.keypoints[k] = Keypoint2D{
                0.42F + 0.08F * ratio,
                simulated_fall_posture ? (0.70F + 0.05F * ratio) : (0.20F + 0.60F * ratio),
                0.92F};
        }
    } else if (simulated_flame_smoke) {
        out_skeleton.semantic_alert_confidence = 0.90F;
    }

    // 鐵律 2：L2 推論結束後立即呼叫 secure_wipe() 強制清零所有 Raw RGB 記憶體並驗證全零
    const WipeReport wipe = tee_ram_buffer.secure_wipe();
    last_wipe_report_     = wipe;
    ++l2_auto_wipe_count_;

    out_skeleton.source_raw_wipe_ns        = wipe.duration_ns;
    out_skeleton.source_raw_wiped_verified = wipe.verified_all_zero && wipe.within_sla_100ms;

    if (!wipe.verified_all_zero) {
        ++wipe_verification_failures_;
        return false;
    }
    return true;
}

FsmTelemetrySnapshot PrivacyFsm::update(std::uint64_t now_ptp_ns,
                                        const AlignedMultiModalSlice& slice,
                                        const DsFusionResult& ds_result,
                                        bool dual_custody_authorized,
                                        float npu_load_pct,
                                        float memory_bw_load_pct,
                                        TeeRawRgbRing& tee_ram_buffer) noexcept {
    last_now_ptp_ns_ = now_ptp_ns;
    update_degradation_state(npu_load_pct, memory_bw_load_pct);

    const bool rf_disturbed =
        (slice.has_csi &&
         (static_cast<double>(slice.csi.fresnel_variance) >= config_.l0_to_l1_csi_variance_threshold ||
          slice.csi.macro_motion_energy >= 0.35F)) ||
        (slice.has_thermal &&
         (slice.thermal.max_temp_c >= 55.0F || slice.thermal.delta_temp_rate_c_per_s >= 0.10F));

    const bool l2_trigger =
        ds_result.high_conflict_escalation ||
        (ds_result.belief_alert >= config_.l1_to_l2_belief_alert_threshold) ||
        (ds_result.plausibility_alert >= 0.72 && ds_result.fused_mass.uncertainty >= 0.45 &&
         ds_result.belief_alert >= 0.25);

    const bool l3_critical_sample =
        ds_result.belief_alert >= config_.l2_to_l3_belief_alert_threshold;

    const PrivacyLevel old_level = level_;

    switch (level_) {
        case PrivacyLevel::L0_AmbientRf: {
            consecutive_critical_frames_ = 0U;
            cooldown_active_             = false;
            if (dual_custody_authorized) {
                level_       = PrivacyLevel::L3_EncryptedForensics;
                last_reason_ = FsmTransitionReason::L2ToL3_DualCustodyAuthorized;
            } else if (rf_disturbed || l2_trigger) {
                // 嚴格遵守 L0 -> L1 漸進喚醒（不跳過非視覺驗證階段）
                level_       = PrivacyLevel::L1_NonVisualVerify;
                last_reason_ = FsmTransitionReason::L0ToL1_RfDisturbance;
            }
            break;
        }

        case PrivacyLevel::L1_NonVisualVerify: {
            consecutive_critical_frames_ = 0U;
            if (dual_custody_authorized) {
                cooldown_active_ = false;
                level_           = PrivacyLevel::L3_EncryptedForensics;
                last_reason_     = FsmTransitionReason::L2ToL3_DualCustodyAuthorized;
            } else if (l2_trigger) {
                cooldown_active_ = false;
                level_           = PrivacyLevel::L2_DeidentifiedRam;
                last_reason_     = FsmTransitionReason::L1ToL2_NonVisualAlertOrConflict;
            } else {
                const bool calm = !rf_disturbed &&
                                  (ds_result.belief_alert < config_.downgrade_safe_belief_threshold) &&
                                  !ds_result.high_conflict_escalation;
                if (calm) {
                    if (!cooldown_active_) {
                        cooldown_active_       = true;
                        cooldown_start_ptp_ns_ = now_ptp_ns;
                        last_reason_           = FsmTransitionReason::Hold_HysteresisCooldownActive;
                    } else if (now_ptp_ns >= cooldown_start_ptp_ns_ &&
                               (now_ptp_ns - cooldown_start_ptp_ns_) >= config_.hysteresis_ns) {
                        level_           = PrivacyLevel::L0_AmbientRf;
                        cooldown_active_ = false;
                        last_reason_     = FsmTransitionReason::Downgrade_HysteresisComplete;
                    } else {
                        last_reason_ = FsmTransitionReason::Hold_HysteresisCooldownActive;
                    }
                } else {
                    cooldown_active_ = false;
                }
            }
            break;
        }

        case PrivacyLevel::L2_DeidentifiedRam: {
            if (dual_custody_authorized) {
                cooldown_active_ = false;
                level_           = PrivacyLevel::L3_EncryptedForensics;
                last_reason_     = FsmTransitionReason::L2ToL3_DualCustodyAuthorized;
            } else if (l3_critical_sample) {
                cooldown_active_ = false;
                ++consecutive_critical_frames_;
                if (consecutive_critical_frames_ >= config_.critical_confirm_frames) {
                    level_       = PrivacyLevel::L3_EncryptedForensics;
                    last_reason_ = FsmTransitionReason::L2ToL3_CriticalConfirmed;
                }
            } else {
                consecutive_critical_frames_ = 0U;
                const bool calm =
                    !ds_result.high_conflict_escalation &&
                    (ds_result.belief_alert < config_.downgrade_safe_belief_threshold);
                if (calm) {
                    if (!cooldown_active_) {
                        cooldown_active_       = true;
                        cooldown_start_ptp_ns_ = now_ptp_ns;
                        last_reason_           = FsmTransitionReason::Hold_HysteresisCooldownActive;
                    } else if (now_ptp_ns >= cooldown_start_ptp_ns_ &&
                               (now_ptp_ns - cooldown_start_ptp_ns_) >= config_.hysteresis_ns) {
                        level_           = PrivacyLevel::L1_NonVisualVerify;
                        cooldown_active_ = false;
                        last_reason_     = FsmTransitionReason::Downgrade_HysteresisComplete;
                    } else {
                        last_reason_ = FsmTransitionReason::Hold_HysteresisCooldownActive;
                    }
                } else {
                    cooldown_active_ = false;
                }
            }
            break;
        }

        case PrivacyLevel::L3_EncryptedForensics: {
            const bool calm = !dual_custody_authorized && !ds_result.high_conflict_escalation &&
                              (ds_result.belief_alert < config_.l2_to_l3_belief_alert_threshold);
            if (calm) {
                if (!cooldown_active_) {
                    cooldown_active_       = true;
                    cooldown_start_ptp_ns_ = now_ptp_ns;
                    last_reason_           = FsmTransitionReason::Hold_HysteresisCooldownActive;
                } else if (now_ptp_ns >= cooldown_start_ptp_ns_ &&
                           (now_ptp_ns - cooldown_start_ptp_ns_) >= config_.hysteresis_ns) {
                    level_                       = PrivacyLevel::L2_DeidentifiedRam;
                    cooldown_active_             = false;
                    consecutive_critical_frames_ = 0U;
                    last_reason_                 = FsmTransitionReason::Downgrade_HysteresisComplete;
                } else {
                    last_reason_ = FsmTransitionReason::Hold_HysteresisCooldownActive;
                }
            } else {
                cooldown_active_ = false;
            }
            break;
        }
    }

    handle_downgrade_wipe(old_level, level_, tee_ram_buffer);
    return snapshot(now_ptp_ns);
}

FsmTelemetrySnapshot PrivacyFsm::snapshot(std::uint64_t now_ptp_ns) const noexcept {
    const HardwareSpec& spec = get_hardware_spec(config_.hardware_tier);
    const std::uint64_t ref_now = (now_ptp_ns != 0ULL) ? now_ptp_ns : last_now_ptp_ns_;

    FsmTelemetrySnapshot snap{};
    snap.level                       = level_;
    snap.last_reason                 = last_reason_;
    snap.camera_capture_allowed      = can_capture_raw_rgb();
    snap.graceful_degradation_active = graceful_degradation_active_;
    snap.hysteresis_cooldown_active  = cooldown_active_;

    if (cooldown_active_ && ref_now >= cooldown_start_ptp_ns_) {
        const std::uint64_t elapsed = ref_now - cooldown_start_ptp_ns_;
        const std::uint64_t rem_ns  = (config_.hysteresis_ns > elapsed)
                                          ? (config_.hysteresis_ns - elapsed)
                                          : 0ULL;
        snap.hysteresis_remaining_sec = static_cast<double>(rem_ns) * 1.0e-9;
    }

    snap.rejected_raw_capture_count = rejected_raw_capture_count_;
    snap.l2_auto_wipe_count         = l2_auto_wipe_count_;
    snap.wipe_verification_failures = wipe_verification_failures_;
    snap.last_wipe_report           = last_wipe_report_;

    switch (level_) {
        case PrivacyLevel::L0_AmbientRf:
            snap.effective_rgb_fps         = 0U;
            snap.effective_mmwave_hz       = 0U;  // LPDS 休眠
            snap.effective_thermal_hz      = 0U;
            snap.effective_csi_hz          = 100U;
            snap.estimated_internal_bw_mbps = 0.8F;
            snap.estimated_uplink_bw_kbps   = 3.5F;
            snap.estimated_incremental_watts = spec.l0_incremental_watts;
            snap.estimated_total_ap_watts    = spec.ap_base_idle_watts + spec.l0_incremental_watts;
            snap.privacy_compliance_tier     = 0U;
            snap.zero_disk_persistence       = true;
            break;

        case PrivacyLevel::L1_NonVisualVerify:
            snap.effective_rgb_fps         = 0U;
            snap.effective_mmwave_hz       = graceful_degradation_active_ ? 20U : 10U;
            snap.effective_thermal_hz      = 9U;
            snap.effective_csi_hz          = 200U;
            snap.estimated_internal_bw_mbps = 2.9F;
            snap.estimated_uplink_bw_kbps   = 12.0F;
            snap.estimated_incremental_watts = spec.l1_incremental_watts;
            snap.estimated_total_ap_watts    = spec.ap_base_active_watts + spec.l1_incremental_watts;
            snap.privacy_compliance_tier     = 1U;
            snap.zero_disk_persistence       = true;
            break;

        case PrivacyLevel::L2_DeidentifiedRam:
            snap.effective_rgb_fps =
                graceful_degradation_active_ ? spec.degraded_rgb_fps : spec.normal_rgb_fps;
            snap.effective_mmwave_hz  = 20U;  // 降載期間強制維持 20 Hz 雷達追蹤
            snap.effective_thermal_hz = 15U;
            snap.effective_csi_hz     = 500U;
            snap.estimated_internal_bw_mbps =
                graceful_degradation_active_ ? 380.0F : 1120.0F;
            snap.estimated_uplink_bw_kbps   = 38.0F;
            snap.estimated_incremental_watts = spec.l2_incremental_watts;
            snap.estimated_total_ap_watts    = spec.ap_base_active_watts + spec.l2_incremental_watts;
            snap.privacy_compliance_tier     = 2U;
            snap.zero_disk_persistence       = true;
            break;

        case PrivacyLevel::L3_EncryptedForensics:
            snap.effective_rgb_fps =
                graceful_degradation_active_ ? spec.degraded_rgb_fps : spec.normal_rgb_fps;
            snap.effective_mmwave_hz        = 20U;
            snap.effective_thermal_hz       = 30U;
            snap.effective_csi_hz           = 1000U;
            snap.estimated_internal_bw_mbps = 2200.0F;
            snap.estimated_uplink_bw_kbps   = 20'000.0F;  // 20 Mbps AES-256-GCM H.265 加密存證
            snap.estimated_incremental_watts = spec.l3_incremental_watts;
            snap.estimated_total_ap_watts    = spec.ap_base_active_watts + spec.l3_incremental_watts;
            snap.privacy_compliance_tier     = 3U;
            snap.zero_disk_persistence       = false;  // 受控加密存證託管
            break;
    }

    snap.poe_budget_exceeded = (snap.estimated_total_ap_watts > spec.poe_pd_budget_watts);
    return snap;
}

// ============================================================================
// 4. 五大場景策略實作（A1, C4, B1, C1, B2）
// ============================================================================

// --- A1: 銀髮居家與長照衛浴／臥室零穿戴跌倒守護 ---
const ScenarioConfig& ScenarioA1ElderlyCare::config() const noexcept {
    static constexpr ScenarioConfig kCfg{
        ScenarioId::A1_ElderlyCare,
        "A1",
        "銀髮居家／長照衛浴零穿戴跌倒與呼吸守護",
        0.55, 0.95, 0.85, 0.90,
        0.55, 0.80};
    return kCfg;
}

std::array<ModalityEvidence, kMaxDsSources> ScenarioA1ElderlyCare::build_evidence(
    const AlignedMultiModalSlice& slice) const noexcept {
    const auto& cfg = config();
    std::array<ModalityEvidence, kMaxDsSources> out{{
        {ModalityId::WirelessCsi, false, cfg.weight_csi, {}, {}},
        {ModalityId::MmWaveRadar, false, cfg.weight_mmwave, {}, {}},
        {ModalityId::ThermalIr, false, cfg.weight_thermal, {}, {}},
        {ModalityId::RgbVision, false, cfg.weight_rgb, {}, {}}
    }};

    if (slice.has_csi) {
        out[0].present           = true;
        out[0].reliability_alpha = cfg.weight_csi * static_cast<double>(clamp01_f(slice.csi.signal_quality));
        const double var         = static_cast<double>(slice.csi.fresnel_variance);
        if (var >= 0.45) {
            out[0].raw_bpa = MassFunction{0.10, 0.65, 0.25};
        } else if (var >= 0.25) {
            out[0].raw_bpa = MassFunction{0.25, 0.40, 0.35};
        } else {
            out[0].raw_bpa = MassFunction{0.75, 0.05, 0.20};
        }
    }

    if (slice.has_mmwave) {
        out[1].present = true;
        out[1].reliability_alpha =
            cfg.weight_mmwave * static_cast<double>(clamp01_f(slice.mmwave.signal_quality));
        const bool fall_kinematics =
            (slice.mmwave.micro_doppler_fall_score >= 0.65F) ||
            (slice.mmwave.height_drop_rate_mps <= -1.0F && slice.mmwave.target_z_m <= 0.50F);
        const bool apnea_or_tachypnea =
            (slice.mmwave.respiration_bpm > 0.0F) &&
            (slice.mmwave.respiration_bpm < 8.0F || slice.mmwave.respiration_bpm > 30.0F);
        if (fall_kinematics && apnea_or_tachypnea) {
            out[1].raw_bpa = MassFunction{0.02, 0.94, 0.04};
        } else if (fall_kinematics || apnea_or_tachypnea) {
            out[1].raw_bpa = MassFunction{0.08, 0.80, 0.12};
        } else if (slice.mmwave.target_z_m <= 0.45F && slice.mmwave.micro_doppler_fall_score < 0.40F) {
            // 低矮且無下墜微都卜勒（疑似寵物或低矮物件）
            out[1].raw_bpa = MassFunction{0.65, 0.10, 0.25};
        } else {
            out[1].raw_bpa = MassFunction{0.82, 0.04, 0.14};
        }
    }

    if (slice.has_thermal) {
        out[2].present = true;
        out[2].reliability_alpha =
            cfg.weight_thermal * static_cast<double>(clamp01_f(slice.thermal.signal_quality));
        const bool human_temp =
            (slice.thermal.max_temp_c >= 31.5F && slice.thermal.max_temp_c <= 41.0F &&
             slice.thermal.human_thermal_score >= 0.50F);
        if (human_temp && slice.has_mmwave && slice.mmwave.target_z_m <= 0.50F) {
            out[2].raw_bpa = MassFunction{0.05, 0.85, 0.10};
        } else if (!human_temp && slice.thermal.max_temp_c < 29.0F) {
            // 低溫無生物熱源（棉被/抱枕）
            out[2].raw_bpa = MassFunction{0.88, 0.02, 0.10};
        } else if (slice.thermal.human_thermal_score < 0.35F) {
            // 高濕水蒸氣或無人形熱輪廓
            out[2].raw_bpa = MassFunction{0.75, 0.05, 0.20};
        } else {
            out[2].raw_bpa = MassFunction{0.65, 0.10, 0.25};
        }
    }

    if (slice.has_rgb_skeleton) {
        out[3].present = true;
        out[3].reliability_alpha =
            cfg.weight_rgb * static_cast<double>(clamp01_f(slice.rgb_skeleton.signal_quality));
        if (slice.rgb_skeleton.posture_fall_detected) {
            out[3].raw_bpa = MassFunction{0.03, 0.90, 0.07};
        } else {
            out[3].raw_bpa = MassFunction{0.82, 0.05, 0.13};
        }
    }
    return out;
}

ScenarioEvaluation ScenarioA1ElderlyCare::evaluate(const AlignedMultiModalSlice& slice,
                                                   const DsFusionResult& ds_result,
                                                   PrivacyLevel /*current_level*/) const noexcept {
    const auto& cfg = config();
    ScenarioEvaluation ev{};
    ev.id               = id();
    ev.scenario_code    = code();
    ev.confidence_score = ds_result.belief_alert;

    // 防誤報 1：浴室水蒸氣／熱氣擾動（CSI 擾動 + 熱像均勻溫熱但無人類輪廓 + 雷達無跌落特徵）
    const bool water_vapor =
        slice.has_csi && slice.csi.fresnel_variance >= 0.35F &&
        slice.has_thermal && slice.thermal.max_temp_c >= 34.0F &&
        slice.thermal.human_thermal_score < 0.35F &&
        (!slice.has_mmwave || slice.mmwave.micro_doppler_fall_score < 0.40F);

    // 防誤報 2：掉落棉被／掃地機／小型寵物
    const bool cold_object =
        slice.has_mmwave && slice.mmwave.target_z_m <= 0.50F &&
        slice.has_thermal && slice.thermal.max_temp_c < 29.0F &&
        slice.thermal.human_thermal_score < 0.35F;

    const bool small_pet =
        slice.has_mmwave && slice.mmwave.target_z_m <= 0.45F &&
        slice.mmwave.height_drop_rate_mps > -0.50F &&
        slice.mmwave.micro_doppler_fall_score < 0.45F &&
        slice.has_thermal && slice.thermal.target_bbox_aspect_ratio > 1.25F;

    if (water_vapor) {
        ev.false_alarm_suppressed = true;
        ev.suppression_reason     = SuppressionReason::WaterVaporFiltered;
        ev.severity               = AlertSeverity::Safe;
        ev.status_summary_zh      = "已抑制誤報：衛浴水蒸氣擾動（無跌倒落差與人體輪廓）";
        return ev;
    }

    if (cold_object || small_pet) {
        ev.false_alarm_suppressed = true;
        ev.suppression_reason     = SuppressionReason::PetOrColdObjectFiltered;
        ev.severity               = AlertSeverity::Safe;
        ev.status_summary_zh      = "已抑制誤報：低溫掉落物或小型寵物活動過濾";
        return ev;
    }

    if (ds_result.belief_alert >= cfg.critical_belief_threshold) {
        ev.severity          = AlertSeverity::Critical;
        ev.alert_triggered   = true;
        ev.actuator_action   = ActuatorCommand::NurseCallAlert;
        ev.status_summary_zh = "危急告警：確認人員跌倒或呼吸異常，已觸發護理鈴呼救";
    } else if (ds_result.belief_alert >= cfg.warning_belief_threshold || ds_result.high_conflict_escalation) {
        ev.severity          = AlertSeverity::Warning;
        ev.alert_triggered   = true;
        ev.actuator_action   = ActuatorCommand::NurseCallAlert;
        ev.status_summary_zh = "高風險預警：疑似跌倒或高衝突訊號，升階交叉驗證";
    } else {
        ev.severity          = AlertSeverity::Safe;
        ev.status_summary_zh = "正常守護：L0/L1 無跌倒異常";
    }
    return ev;
}

// --- C4: BESS 儲能站／EV 超充站熱失控 Stage 1 截斷與濃煙搜救 ---
const ScenarioConfig& ScenarioC4BessThermal::config() const noexcept {
    static constexpr ScenarioConfig kCfg{
        ScenarioId::C4_BessThermal,
        "C4",
        "BESS 儲能站／EV 超充熱失控 Stage 1 截斷與濃煙搜救",
        0.45, 0.85, 0.96, 0.80,
        0.55, 0.78};
    return kCfg;
}

std::array<ModalityEvidence, kMaxDsSources> ScenarioC4BessThermal::build_evidence(
    const AlignedMultiModalSlice& slice) const noexcept {
    const auto& cfg = config();
    std::array<ModalityEvidence, kMaxDsSources> out{{
        {ModalityId::WirelessCsi, false, cfg.weight_csi, {}, {}},
        {ModalityId::MmWaveRadar, false, cfg.weight_mmwave, {}, {}},
        {ModalityId::ThermalIr, false, cfg.weight_thermal, {}, {}},
        {ModalityId::RgbVision, false, cfg.weight_rgb, {}, {}}
    }};

    if (slice.has_csi) {
        out[0].present           = true;
        out[0].reliability_alpha = cfg.weight_csi * static_cast<double>(clamp01_f(slice.csi.signal_quality));
        out[0].raw_bpa = (slice.csi.fresnel_variance >= 0.40F)
                             ? MassFunction{0.20, 0.45, 0.35}
                             : MassFunction{0.60, 0.05, 0.35};
    }

    if (slice.has_mmwave) {
        out[1].present = true;
        out[1].reliability_alpha =
            cfg.weight_mmwave * static_cast<double>(clamp01_f(slice.mmwave.signal_quality));
        const bool person_in_hazard =
            (slice.mmwave.point_count > 0U) &&
            (slice.mmwave.gait_human_likelihood >= 0.55F || slice.mmwave.respiration_bpm >= 8.0F);
        out[1].raw_bpa = person_in_hazard
                             ? MassFunction{0.10, 0.60, 0.30}
                             : MassFunction{0.35, 0.05, 0.60};  // 雷達無法直接測溫，保持較高不確定性
    }

    if (slice.has_thermal) {
        out[2].present = true;
        out[2].reliability_alpha =
            cfg.weight_thermal * static_cast<double>(clamp01_f(slice.thermal.signal_quality));
        const float max_t = slice.thermal.max_temp_c;
        const float rate  = slice.thermal.delta_temp_rate_c_per_s;
        if (max_t >= 70.0F || rate >= 0.25F) {
            out[2].raw_bpa = MassFunction{0.02, 0.94, 0.04};
        } else if (max_t >= 60.0F || rate >= 0.15F) {
            out[2].raw_bpa = MassFunction{0.08, 0.80, 0.12};
        } else {
            out[2].raw_bpa = MassFunction{0.88, 0.03, 0.09};
        }
    }

    if (slice.has_rgb_skeleton && slice.rgb_skeleton.signal_quality >= 0.25F) {
        out[3].present = true;
        out[3].reliability_alpha =
            cfg.weight_rgb * static_cast<double>(clamp01_f(slice.rgb_skeleton.signal_quality));
        if (slice.rgb_skeleton.flame_or_smoke_detected) {
            out[3].raw_bpa = MassFunction{0.02, 0.92, 0.06};
        } else if (slice.rgb_skeleton.vehicle_heat_source_mask) {
            // 語意確認高溫點位於煞車碟盤或陽光照射深色車頂，非電池櫃異常
            out[3].raw_bpa = MassFunction{0.85, 0.05, 0.10};
        } else {
            out[3].raw_bpa = MassFunction{0.70, 0.10, 0.20};
        }
    }
    return out;
}

ScenarioEvaluation ScenarioC4BessThermal::evaluate(const AlignedMultiModalSlice& slice,
                                                   const DsFusionResult& ds_result,
                                                   PrivacyLevel /*current_level*/) const noexcept {
    const auto& cfg = config();
    ScenarioEvaluation ev{};
    ev.id               = id();
    ev.scenario_code    = code();
    ev.confidence_score = ds_result.belief_alert;

    // 防誤報：車輛煞車碟盤/引擎蓋餘熱或夏季車頂日曬（高溫但無正溫升斜率 + RGB 部位遮罩確認）
    const bool vehicle_non_battery_heat =
        slice.has_thermal && slice.thermal.max_temp_c >= 55.0F &&
        slice.thermal.delta_temp_rate_c_per_s < 0.05F &&
        slice.has_rgb_skeleton && slice.rgb_skeleton.vehicle_heat_source_mask &&
        !slice.rgb_skeleton.flame_or_smoke_detected;

    if (vehicle_non_battery_heat) {
        ev.false_alarm_suppressed = true;
        ev.suppression_reason     = SuppressionReason::VehicleExhaustOrSunGlintFiltered;
        ev.severity               = AlertSeverity::Safe;
        ev.status_summary_zh      = "已抑制誤報：確認為車輛煞車餘熱或日照反光（非電池熱失控）";
        return ev;
    }

    const bool stage1_thermal_crit =
        slice.has_thermal &&
        (slice.thermal.max_temp_c >= 70.0F || slice.thermal.delta_temp_rate_c_per_s >= 0.20F);
    const bool stage1_thermal_warn =
        slice.has_thermal &&
        (slice.thermal.max_temp_c >= 60.0F || slice.thermal.delta_temp_rate_c_per_s >= 0.12F);
    const bool smoke_rescue_person =
        slice.has_mmwave && slice.mmwave.point_count > 0U &&
        (slice.mmwave.gait_human_likelihood >= 0.55F || slice.mmwave.respiration_bpm >= 8.0F);

    if (ds_result.belief_alert >= cfg.critical_belief_threshold || stage1_thermal_crit) {
        ev.severity        = AlertSeverity::Critical;
        ev.alert_triggered = true;
        ev.actuator_action = ActuatorCommand::BmsEmergencyTrip;
        ev.status_summary_zh = smoke_rescue_person
                                   ? "危急告警：熱失控 Stage 1 觸發 BMS 急停，並於濃煙中雷達鎖定受困人員"
                                   : "危急告警：電池/配電熱失控觸發 BMS 斷電跳脫";
    } else if (ds_result.belief_alert >= cfg.warning_belief_threshold || stage1_thermal_warn ||
               ds_result.high_conflict_escalation) {
        ev.severity          = AlertSeverity::Warning;
        ev.alert_triggered   = true;
        ev.actuator_action   = ActuatorCommand::BmsEmergencyTrip;
        ev.status_summary_zh = "Stage 1 異常溫升預警：建議立即降載/切斷充電迴路";
    } else {
        ev.severity          = AlertSeverity::Safe;
        ev.status_summary_zh = "儲能站熱場正常";
    }
    return ev;
}

// --- B1: 關鍵園區／倉儲全天候零誤報周界入侵偵測 ---
const ScenarioConfig& ScenarioB1Perimeter::config() const noexcept {
    static constexpr ScenarioConfig kCfg{
        ScenarioId::B1_Perimeter,
        "B1",
        "關鍵園區／倉儲全天候零誤報周界入侵偵測",
        0.75, 0.92, 0.88, 0.90,
        0.55, 0.80};
    return kCfg;
}

std::array<ModalityEvidence, kMaxDsSources> ScenarioB1Perimeter::build_evidence(
    const AlignedMultiModalSlice& slice) const noexcept {
    const auto& cfg = config();
    std::array<ModalityEvidence, kMaxDsSources> out{{
        {ModalityId::WirelessCsi, false, cfg.weight_csi, {}, {}},
        {ModalityId::MmWaveRadar, false, cfg.weight_mmwave, {}, {}},
        {ModalityId::ThermalIr, false, cfg.weight_thermal, {}, {}},
        {ModalityId::RgbVision, false, cfg.weight_rgb, {}, {}}
    }};

    const bool authorized_patrol =
        slice.has_csi && slice.csi.whitelist_tag_present && slice.csi.whitelist_auth_level >= 2U;

    if (slice.has_csi) {
        out[0].present           = true;
        out[0].reliability_alpha = cfg.weight_csi * static_cast<double>(clamp01_f(slice.csi.signal_quality));
        if (authorized_patrol) {
            out[0].raw_bpa = MassFunction{0.93, 0.02, 0.05};
        } else if (slice.csi.fresnel_variance >= 0.30F) {
            out[0].raw_bpa = MassFunction{0.10, 0.65, 0.25};
        } else {
            out[0].raw_bpa = MassFunction{0.80, 0.05, 0.15};
        }
    }

    if (slice.has_mmwave) {
        out[1].present = true;
        out[1].reliability_alpha =
            cfg.weight_mmwave * static_cast<double>(clamp01_f(slice.mmwave.signal_quality));
        const double speed = std::abs(static_cast<double>(slice.mmwave.radial_velocity_mps));
        if (authorized_patrol) {
            out[1].raw_bpa = MassFunction{0.85, 0.03, 0.12};  // 軌跡與 UWB/BLE 白名單核銷吻合
        } else if (slice.mmwave.gait_human_likelihood >= 0.65F && speed >= 0.30 && speed <= 6.0) {
            out[1].raw_bpa = MassFunction{0.05, 0.86, 0.09};
        } else if (slice.mmwave.gait_human_likelihood < 0.45F) {
            out[1].raw_bpa = MassFunction{0.82, 0.05, 0.13};
        } else {
            out[1].raw_bpa = MassFunction{0.40, 0.30, 0.30};
        }
    }

    if (slice.has_thermal) {
        out[2].present = true;
        out[2].reliability_alpha =
            cfg.weight_thermal * static_cast<double>(clamp01_f(slice.thermal.signal_quality));
        const bool vertical_human_heat =
            (slice.thermal.max_temp_c >= 30.0F && slice.thermal.human_thermal_score >= 0.60F &&
             slice.thermal.target_bbox_aspect_ratio <= 0.90F);
        if (authorized_patrol) {
            out[2].raw_bpa = MassFunction{0.82, 0.03, 0.15};
        } else if (vertical_human_heat) {
            out[2].raw_bpa = MassFunction{0.06, 0.84, 0.10};
        } else if (slice.thermal.human_thermal_score < 0.40F ||
                   slice.thermal.target_bbox_aspect_ratio > 1.15F) {
            out[2].raw_bpa = MassFunction{0.84, 0.04, 0.12};
        } else {
            out[2].raw_bpa = MassFunction{0.55, 0.20, 0.25};
        }
    }

    if (slice.has_rgb_skeleton) {
        out[3].present = true;
        out[3].reliability_alpha =
            cfg.weight_rgb * static_cast<double>(clamp01_f(slice.rgb_skeleton.signal_quality));
        out[3].raw_bpa = slice.rgb_skeleton.person_detected
                             ? MassFunction{0.05, 0.88, 0.07}
                             : MassFunction{0.88, 0.03, 0.09};
    }
    return out;
}

ScenarioEvaluation ScenarioB1Perimeter::evaluate(const AlignedMultiModalSlice& slice,
                                                 const DsFusionResult& ds_result,
                                                 PrivacyLevel /*current_level*/) const noexcept {
    const auto& cfg = config();
    ScenarioEvaluation ev{};
    ev.id               = id();
    ev.scenario_code    = code();
    ev.confidence_score = ds_result.belief_alert;

    // 防誤報 1：授權巡檢人員攜帶加密 UWB/BLE 白名單信標
    if (slice.has_csi && slice.csi.whitelist_tag_present && slice.csi.whitelist_auth_level >= 2U) {
        ev.false_alarm_suppressed = true;
        ev.suppression_reason     = SuppressionReason::AuthorizedWhitelistMatched;
        ev.severity               = AlertSeverity::Safe;
        ev.status_summary_zh      = "已自動核銷：授權巡檢人員白名單（UWB/BLE AoA 驗證）";
        return ev;
    }

    // 防誤報 2：搖晃樹影/強風降雨，或四足野生動物（矮小體高與寬長寬比）
    const bool swaying_foliage =
        slice.has_mmwave && slice.mmwave.gait_human_likelihood < 0.45F &&
        (!slice.has_thermal || slice.thermal.human_thermal_score < 0.40F) &&
        (!slice.has_rgb_skeleton || !slice.rgb_skeleton.person_detected);

    const bool small_animal =
        slice.has_thermal && slice.thermal.max_temp_c >= 30.0F &&
        slice.thermal.target_bbox_aspect_ratio > 1.15F &&
        slice.has_mmwave && slice.mmwave.target_z_m < 0.65F &&
        slice.mmwave.gait_human_likelihood < 0.50F;

    if (swaying_foliage || small_animal) {
        ev.false_alarm_suppressed = true;
        ev.suppression_reason     = SuppressionReason::SwayingTreeOrAnimalFiltered;
        ev.severity               = AlertSeverity::Safe;
        ev.status_summary_zh      = "已抑制誤報：排除樹影晃動、天候雜訊或野生動物干擾";
        return ev;
    }

    if (ds_result.belief_alert >= cfg.critical_belief_threshold) {
        ev.severity          = AlertSeverity::Critical;
        ev.alert_triggered   = true;
        ev.actuator_action   = ActuatorCommand::SecuritySirenAndVms;
        ev.status_summary_zh = "確認入侵告警：非白名單人員翻越周界，已觸發 VMS 與聲光警示";
    } else if (ds_result.belief_alert >= cfg.warning_belief_threshold || ds_result.high_conflict_escalation) {
        ev.severity          = AlertSeverity::Warning;
        ev.alert_triggered   = true;
        ev.actuator_action   = ActuatorCommand::SecuritySirenAndVms;
        ev.status_summary_zh = "周界疑似入侵：升階擷取去識別化影像特徵確認";
    } else {
        ev.severity          = AlertSeverity::Safe;
        ev.status_summary_zh = "周界安全戒備中";
    }
    return ev;
}

// --- C1: 智慧工廠人機協作（HRC）／粉塵冷鏈盲區動態防撞 ---
const ScenarioConfig& ScenarioC1HrcSafety::config() const noexcept {
    static constexpr ScenarioConfig kCfg{
        ScenarioId::C1_HrcSafety,
        "C1",
        "人機協作 (HRC) 與倉儲粉塵冷鏈盲區動態防撞",
        0.85, 0.96, 0.88, 0.78,
        0.50, 0.78};
    return kCfg;
}

std::array<ModalityEvidence, kMaxDsSources> ScenarioC1HrcSafety::build_evidence(
    const AlignedMultiModalSlice& slice) const noexcept {
    const auto& cfg = config();
    std::array<ModalityEvidence, kMaxDsSources> out{{
        {ModalityId::WirelessCsi, false, cfg.weight_csi, {}, {}},
        {ModalityId::MmWaveRadar, false, cfg.weight_mmwave, {}, {}},
        {ModalityId::ThermalIr, false, cfg.weight_thermal, {}, {}},
        {ModalityId::RgbVision, false, cfg.weight_rgb, {}, {}}
    }};

    if (slice.has_csi) {
        out[0].present           = true;
        out[0].reliability_alpha = cfg.weight_csi * static_cast<double>(clamp01_f(slice.csi.signal_quality));
        if (slice.csi.whitelist_tag_present && slice.csi.whitelist_distance_m < 1.0F) {
            out[0].raw_bpa = MassFunction{0.03, 0.90, 0.07};
        } else if (slice.csi.whitelist_tag_present && slice.csi.whitelist_distance_m <= 2.5F) {
            out[0].raw_bpa = MassFunction{0.15, 0.65, 0.20};
        } else {
            out[0].raw_bpa = MassFunction{0.78, 0.05, 0.17};
        }
    }

    if (slice.has_mmwave) {
        out[1].present = true;
        out[1].reliability_alpha =
            cfg.weight_mmwave * static_cast<double>(clamp01_f(slice.mmwave.signal_quality));
        const double dist = compute_radar_distance_m(slice.mmwave);
        if (dist < 1.0) {
            out[1].raw_bpa = MassFunction{0.03, 0.91, 0.06};
        } else if (dist <= 2.5 && slice.mmwave.radial_velocity_mps < -0.15F) {
            out[1].raw_bpa = MassFunction{0.12, 0.72, 0.16};
        } else {
            out[1].raw_bpa = MassFunction{0.82, 0.05, 0.13};
        }
    }

    if (slice.has_thermal) {
        out[2].present = true;
        out[2].reliability_alpha =
            cfg.weight_thermal * static_cast<double>(clamp01_f(slice.thermal.signal_quality));
        if (slice.thermal.max_temp_c >= 30.0F && slice.thermal.human_thermal_score >= 0.55F) {
            out[2].raw_bpa = MassFunction{0.08, 0.80, 0.12};
        } else if (slice.thermal.max_temp_c < 28.0F && slice.thermal.human_thermal_score < 0.35F) {
            // 非生物冷物體（水霧 / 垂掛晃動電纜 / 空紙箱）
            out[2].raw_bpa = MassFunction{0.88, 0.03, 0.09};
        } else {
            out[2].raw_bpa = MassFunction{0.60, 0.15, 0.25};
        }
    }

    if (slice.has_rgb_skeleton && slice.rgb_skeleton.signal_quality >= 0.25F) {
        out[3].present = true;
        out[3].reliability_alpha =
            cfg.weight_rgb * static_cast<double>(clamp01_f(slice.rgb_skeleton.signal_quality));
        out[3].raw_bpa = slice.rgb_skeleton.person_detected
                             ? MassFunction{0.08, 0.82, 0.10}
                             : MassFunction{0.85, 0.05, 0.10};
    }
    return out;
}

ScenarioEvaluation ScenarioC1HrcSafety::evaluate(const AlignedMultiModalSlice& slice,
                                                 const DsFusionResult& ds_result,
                                                 PrivacyLevel /*current_level*/) const noexcept {
    const auto& cfg = config();
    ScenarioEvaluation ev{};
    ev.id               = id();
    ev.scenario_code    = code();
    ev.confidence_score = ds_result.belief_alert;

    double radar_dist = 99.0;
    if (slice.has_mmwave) {
        radar_dist = compute_radar_distance_m(slice.mmwave);
    }
    double uwb_dist = 99.0;
    if (slice.has_csi && slice.csi.whitelist_tag_present) {
        uwb_dist = static_cast<double>(slice.csi.whitelist_distance_m);
    }
    ev.safety_distance_m = std::min(radar_dist, uwb_dist);

    // 防誤報：水霧、冷鏈凝霧或無生命低溫垂掛電纜晃動（熱像冷背景 + 無 UWB 作業員標籤）
    const bool cold_mist_or_cable =
        slice.has_mmwave && (radar_dist <= 2.5) &&
        slice.has_thermal && slice.thermal.max_temp_c < 28.0F &&
        slice.thermal.human_thermal_score < 0.35F &&
        (!slice.has_csi || !slice.csi.whitelist_tag_present || slice.csi.whitelist_distance_m > 3.5F) &&
        (!slice.has_rgb_skeleton || !slice.rgb_skeleton.person_detected);

    if (cold_mist_or_cable) {
        ev.false_alarm_suppressed = true;
        ev.suppression_reason     = SuppressionReason::WaterMistOrCableSwingFiltered;
        ev.severity               = AlertSeverity::Safe;
        ev.actuator_action        = ActuatorCommand::None;
        ev.status_summary_zh      = "已抑制誤急停：水霧或低溫晃動電纜（非活體作業員）";
        return ev;
    }

    const bool human_confirmed =
        (slice.has_thermal && slice.thermal.human_thermal_score >= 0.55F) ||
        (slice.has_csi && slice.csi.whitelist_tag_present && uwb_dist <= 2.5) ||
        (slice.has_rgb_skeleton && slice.rgb_skeleton.person_detected);

    if (human_confirmed && ev.safety_distance_m < 1.0 &&
        ds_result.belief_alert >= cfg.warning_belief_threshold) {
        ev.severity          = AlertSeverity::Critical;
        ev.alert_triggered   = true;
        ev.actuator_action   = ActuatorCommand::PlcEmergencyStop;
        ev.status_summary_zh = "危急防撞：作業員進入 <1.0m 紅區，下達 PLC 緊急停機 (E-Stop)";
    } else if (human_confirmed && ev.safety_distance_m <= 2.5 &&
               (ds_result.belief_alert >= cfg.warning_belief_threshold ||
                (slice.has_mmwave && slice.mmwave.radial_velocity_mps < -0.20F))) {
        ev.severity          = AlertSeverity::Warning;
        ev.alert_triggered   = true;
        ev.actuator_action   = ActuatorCommand::PlcSafeSlowdown;
        ev.status_summary_zh = "動態防撞警告：進入 1.0-2.5m 警戒區，依 ISO/TS 15066 觸發安全降速";
    } else if (ds_result.high_conflict_escalation) {
        ev.severity          = AlertSeverity::Warning;
        ev.alert_triggered   = true;
        ev.actuator_action   = ActuatorCommand::PlcSafeSlowdown;
        ev.status_summary_zh = "感測高衝突：預防性降速並升階驗證";
    } else {
        ev.severity          = AlertSeverity::Safe;
        ev.status_summary_zh = "協作安全區淨空，全速生產中";
    }
    return ev;
}

// --- B2: 智慧商辦微氣候 HVAC 零 False-Off 節能與夜間安防 ---
const ScenarioConfig& ScenarioB2SmartHvac::config() const noexcept {
    static constexpr ScenarioConfig kCfg{
        ScenarioId::B2_SmartHvac,
        "B2",
        "智慧商辦微氣候 HVAC 零 False-Off 節能與夜間安防",
        0.70, 0.90, 0.88, 0.85,
        0.55, 0.80};
    return kCfg;
}

std::array<ModalityEvidence, kMaxDsSources> ScenarioB2SmartHvac::build_evidence(
    const AlignedMultiModalSlice& slice) const noexcept {
    const auto& cfg = config();
    std::array<ModalityEvidence, kMaxDsSources> out{{
        {ModalityId::WirelessCsi, false, cfg.weight_csi, {}, {}},
        {ModalityId::MmWaveRadar, false, cfg.weight_mmwave, {}, {}},
        {ModalityId::ThermalIr, false, cfg.weight_thermal, {}, {}},
        {ModalityId::RgbVision, false, cfg.weight_rgb, {}, {}}
    }};

    if (slice.has_csi) {
        out[0].present           = true;
        out[0].reliability_alpha = cfg.weight_csi * static_cast<double>(clamp01_f(slice.csi.signal_quality));
        if (slice.csi.fresnel_variance >= 0.45F && !slice.csi.whitelist_tag_present) {
            out[0].raw_bpa = MassFunction{0.15, 0.60, 0.25};  // 非授權劇烈擾動（供夜間安防）
        } else {
            out[0].raw_bpa = MassFunction{0.80, 0.05, 0.15};
        }
    }

    if (slice.has_mmwave) {
        out[1].present = true;
        out[1].reliability_alpha =
            cfg.weight_mmwave * static_cast<double>(clamp01_f(slice.mmwave.signal_quality));
        // 若玻璃阻隔熱像提示走廊路人，降低雷達對「異常入侵/室內存在」之支持
        if (slice.has_thermal && slice.thermal.glass_blocked_hint >= 0.50F) {
            out[1].raw_bpa = MassFunction{0.75, 0.05, 0.20};
        } else if (slice.mmwave.gait_human_likelihood >= 0.70F &&
                   slice.has_csi && !slice.csi.whitelist_tag_present &&
                   slice.csi.fresnel_variance >= 0.45F) {
            out[1].raw_bpa = MassFunction{0.08, 0.76, 0.16};
        } else if (slice.mmwave.respiration_bpm >= 8.0F && slice.mmwave.respiration_bpm <= 28.0F) {
            out[1].raw_bpa = MassFunction{0.78, 0.07, 0.15};  // 靜坐呼吸微動（日常佔用非異常）
        } else {
            out[1].raw_bpa = MassFunction{0.82, 0.04, 0.14};
        }
    }

    if (slice.has_thermal) {
        out[2].present = true;
        out[2].reliability_alpha =
            cfg.weight_thermal * static_cast<double>(clamp01_f(slice.thermal.signal_quality));
        if (slice.thermal.glass_blocked_hint >= 0.50F || slice.thermal.human_thermal_score < 0.30F) {
            out[2].raw_bpa = MassFunction{0.86, 0.03, 0.11};
        } else if (slice.has_csi && !slice.csi.whitelist_tag_present &&
                   slice.csi.fresnel_variance >= 0.45F) {
            out[2].raw_bpa = MassFunction{0.10, 0.55, 0.35};  // 確認為室內真實活體熱源而非玻璃外誤觸
        } else {
            out[2].raw_bpa = MassFunction{0.78, 0.05, 0.17};
        }
    }

    if (slice.has_rgb_skeleton) {
        out[3].present = true;
        out[3].reliability_alpha =
            cfg.weight_rgb * static_cast<double>(clamp01_f(slice.rgb_skeleton.signal_quality));
        out[3].raw_bpa =
            (slice.rgb_skeleton.person_detected &&
             (!slice.has_csi || !slice.csi.whitelist_tag_present))
                ? MassFunction{0.05, 0.85, 0.10}
                : MassFunction{0.88, 0.03, 0.09};
    }
    return out;
}

ScenarioEvaluation ScenarioB2SmartHvac::evaluate(const AlignedMultiModalSlice& slice,
                                                 const DsFusionResult& ds_result,
                                                 PrivacyLevel /*current_level*/) const noexcept {
    const auto& cfg = config();
    ScenarioEvaluation ev{};
    ev.id               = id();
    ev.scenario_code    = code();
    ev.confidence_score = ds_result.belief_alert;

    // 防誤報 1：玻璃隔間外走廊行人（雷達穿玻璃偵測移動，但 LWIR 被普通玻璃阻隔證實室內無人體熱源）
    const bool glass_outside_passerby =
        slice.has_mmwave &&
        (std::abs(slice.mmwave.radial_velocity_mps) > 0.25F ||
         slice.mmwave.gait_human_likelihood >= 0.45F) &&
        slice.has_thermal &&
        (slice.thermal.glass_blocked_hint >= 0.50F || slice.thermal.human_thermal_score < 0.30F) &&
        (!slice.has_csi || slice.csi.fresnel_variance < 0.25F);

    if (glass_outside_passerby) {
        ev.false_alarm_suppressed = true;
        ev.suppression_reason     = SuppressionReason::GlassOutsidePasserbyFiltered;
        ev.severity               = AlertSeverity::Safe;
        ev.actuator_action        = ActuatorCommand::HvacEcoSetback;
        ev.status_summary_zh      = "已抑制誤報：排除玻璃隔間外走廊行人穿牆雷達誤觸，切入 HVAC 節能";
        return ev;
    }

    // 防誤報 2：靜態非人發熱源（暖氣/主機）且無呼吸微動
    const bool static_heater =
        slice.has_thermal && slice.thermal.max_temp_c >= 42.0F &&
        slice.thermal.human_thermal_score < 0.35F &&
        (!slice.has_mmwave || slice.mmwave.respiration_bpm < 5.0F);

    if (static_heater) {
        ev.false_alarm_suppressed = true;
        ev.suppression_reason     = SuppressionReason::StaticHeatSourceFiltered;
        ev.severity               = AlertSeverity::Safe;
        ev.actuator_action        = ActuatorCommand::HvacEcoSetback;
        ev.status_summary_zh      = "已抑制誤報：確認為靜態機器熱源（無呼吸徵象），切入 HVAC 節能";
        return ev;
    }

    // 夜間異常入侵判定
    if (ds_result.belief_alert >= cfg.critical_belief_threshold) {
        ev.severity          = AlertSeverity::Critical;
        ev.alert_triggered   = true;
        ev.actuator_action   = ActuatorCommand::SecuritySirenAndVms;
        ev.status_summary_zh = "非營業時段告警：偵測未授權室內闖入";
        return ev;
    }

    // 零 False-Off 人員佔用維持：即使 CSI 動能極低，只要雷達測得靜坐呼吸且熱像確認室內體溫即維持空調
    const bool stationary_breathing_occupied =
        slice.has_mmwave &&
        slice.mmwave.respiration_bpm >= 8.0F && slice.mmwave.respiration_bpm <= 28.0F &&
        slice.has_thermal && slice.thermal.human_thermal_score >= 0.50F &&
        slice.thermal.glass_blocked_hint < 0.40F;

    if (stationary_breathing_occupied) {
        ev.severity          = AlertSeverity::Safe;
        ev.actuator_action   = ActuatorCommand::HvacComfortHold;
        ev.status_summary_zh = "零 False-Off 確認：偵測靜坐人員呼吸與體熱，維持舒適 HVAC 運轉";
    } else {
        ev.severity          = AlertSeverity::Safe;
        ev.actuator_action   = ActuatorCommand::HvacEcoSetback;
        ev.status_summary_zh = "確認空間無人，觸發 BACnet 5-10 分鐘 HVAC 節能退縮";
    }
    return ev;
}

const IScenarioProfile& get_scenario_profile(ScenarioId id) noexcept {
    static const ScenarioA1ElderlyCare kA1{};
    static const ScenarioC4BessThermal kC4{};
    static const ScenarioB1Perimeter   kB1{};
    static const ScenarioC1HrcSafety   kC1{};
    static const ScenarioB2SmartHvac   kB2{};

    static const std::array<const IScenarioProfile*, 5> kRegistry{{
        &kA1, &kC4, &kB1, &kC1, &kB2
    }};
    const auto idx = static_cast<std::size_t>(id);
    return *kRegistry[idx < kRegistry.size() ? idx : 0U];
}

// ============================================================================
// 5. EdgeFusionCore 整合管線實作
// ============================================================================

void EdgeFusionCore::reset() noexcept {
    aligner_.reset();
    fsm_.reset();
    (void)tee_raw_ring_.secure_wipe();
}

bool EdgeFusionCore::execute_l2_skeleton_and_wipe(RgbSkeletonFrame& out_skeleton,
                                                  bool person_present,
                                                  bool fall_posture,
                                                  bool flame_smoke,
                                                  bool vehicle_heat_mask) noexcept {
    const bool ok = fsm_.run_l2_inference_and_wipe(
        tee_raw_ring_, out_skeleton, person_present, fall_posture, flame_smoke, vehicle_heat_mask);
    if (ok) {
        (void)aligner_.push_rgb_skeleton(out_skeleton);
    }
    return ok;
}

CoreStepOutput EdgeFusionCore::step(const CoreStepInput& input) noexcept {
    const auto t0 = std::chrono::steady_clock::now();

    CoreStepOutput out{};
    const IScenarioProfile& profile = get_scenario_profile(input.scenario);

    out.aligned_slice = aligner_.align(input.target_ptp_ns);
    const auto evidence = profile.build_evidence(out.aligned_slice);
    out.ds_result       = DempsterShaferEngine::combine_all(evidence);

    out.fsm_snapshot = fsm_.update(input.target_ptp_ns,
                                   out.aligned_slice,
                                   out.ds_result,
                                   input.dual_custody_authorized,
                                   input.npu_load_pct,
                                   input.memory_bw_load_pct,
                                   tee_raw_ring_);

    out.evaluation = profile.evaluate(out.aligned_slice, out.ds_result, out.fsm_snapshot.level);

    const auto t1 = std::chrono::steady_clock::now();
    out.processing_latency_ns = static_cast<std::uint64_t>(
        std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count());
    return out;
}

}  // namespace edge_fusion
