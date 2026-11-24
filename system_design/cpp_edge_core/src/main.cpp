/**
 * @file main.cpp
 * @brief 四模態 C++20 嵌入式邊緣融合核心示範程式與微基準測試（Zero-Heap Hot Path）
 *
 * 演練內容：
 * 1. 強型別 POD/Trivial 結構與三階硬體規格表展示。
 * 2. Top 5 旗艦應用（A1, C4, B1, C1, B2）真實觸發與物理防誤報對照演練。
 * 3. Privacy-by-Design L0 -> L1 -> L2 -> L3 狀態機、Camera-Off 守衛、L2 骨架提取與
 *    <100 ms 強制安全清零（secure_wipe()）實測、以及 NPU 滿載 Graceful Degradation。
 * 4. 10,000 次熱路徑管線微基準測試（Microsecond 級端側運算延遲）。
 */

#include "ds_fusion.hpp"
#include "privacy_fsm.hpp"
#include "scenario_profiles.hpp"
#include "secure_ring_buffer.hpp"
#include "sensor_types.hpp"
#include "sliding_window.hpp"

#include <algorithm>
#include <array>
#include <chrono>
#include <cstdint>
#include <cstring>
#include <iomanip>
#include <iostream>
#include <string_view>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace {

using namespace edge_fusion;

[[nodiscard]] std::string_view level_to_str(PrivacyLevel lvl) noexcept {
    switch (lvl) {
        case PrivacyLevel::L0_AmbientRf:
            return "L0 (常駐 RF / 鏡頭關閉)";
        case PrivacyLevel::L1_NonVisualVerify:
            return "L1 (雷達+熱像非視覺驗證)";
        case PrivacyLevel::L2_DeidentifiedRam:
            return "L2 (TEE RAM 17點骨架+清零)";
        case PrivacyLevel::L3_EncryptedForensics:
            return "L3 (AES-256-GCM 加密存證)";
    }
    return "Unknown";
}

[[nodiscard]] std::string_view actuator_to_str(ActuatorCommand cmd) noexcept {
    switch (cmd) {
        case ActuatorCommand::None:
            return "None (無動作)";
        case ActuatorCommand::NurseCallAlert:
            return "NurseCallAlert (護理鈴 MQTT/SIP)";
        case ActuatorCommand::BmsEmergencyTrip:
            return "BmsEmergencyTrip (BMS 斷電急停)";
        case ActuatorCommand::SecuritySirenAndVms:
            return "SecuritySirenAndVms (圍籬告警+VMS)";
        case ActuatorCommand::PlcSafeSlowdown:
            return "PlcSafeSlowdown (ISO/TS 15066 降速)";
        case ActuatorCommand::PlcEmergencyStop:
            return "PlcEmergencyStop (PLC 緊急停機)";
        case ActuatorCommand::HvacComfortHold:
            return "HvacComfortHold (維持舒適空調)";
        case ActuatorCommand::HvacEcoSetback:
            return "HvacEcoSetback (HVAC 節能退縮)";
    }
    return "Unknown";
}

[[nodiscard]] std::string_view suppression_to_str(SuppressionReason r) noexcept {
    switch (r) {
        case SuppressionReason::None:
            return "無";
        case SuppressionReason::PetOrColdObjectFiltered:
            return "PetOrColdObjectFiltered (寵物/低溫掉落物過濾)";
        case SuppressionReason::WaterVaporFiltered:
            return "WaterVaporFiltered (衛浴水蒸氣過濾)";
        case SuppressionReason::VehicleExhaustOrSunGlintFiltered:
            return "VehicleExhaustOrSunGlintFiltered (煞車餘熱/日照過濾)";
        case SuppressionReason::AuthorizedWhitelistMatched:
            return "AuthorizedWhitelistMatched (UWB/BLE 白名單核銷)";
        case SuppressionReason::SwayingTreeOrAnimalFiltered:
            return "SwayingTreeOrAnimalFiltered (樹影/動物過濾)";
        case SuppressionReason::WaterMistOrCableSwingFiltered:
            return "WaterMistOrCableSwingFiltered (水霧/晃動電纜過濾)";
        case SuppressionReason::GlassOutsidePasserbyFiltered:
            return "GlassOutsidePasserbyFiltered (玻璃外走廊行人過濾)";
        case SuppressionReason::StaticHeatSourceFiltered:
            return "StaticHeatSourceFiltered (靜態非人熱源過濾)";
    }
    return "Unknown";
}

void print_separator() {
    std::cout << "================================================================================\n";
}

void print_hardware_table() {
    print_separator();
    std::cout << "[1] 強型別零動態配置感測幀與三階硬體配置表\n";
    std::cout << "  - CsiFrame:         " << sizeof(CsiFrame) << " bytes (trivially_copyable="
              << std::is_trivially_copyable_v<CsiFrame> << ")\n";
    std::cout << "  - MmWaveFrame:      " << sizeof(MmWaveFrame) << " bytes (trivially_copyable="
              << std::is_trivially_copyable_v<MmWaveFrame> << ")\n";
    std::cout << "  - ThermalFrame:     " << sizeof(ThermalFrame) << " bytes (trivially_copyable="
              << std::is_trivially_copyable_v<ThermalFrame> << ")\n";
    std::cout << "  - RgbSkeletonFrame: " << sizeof(RgbSkeletonFrame) << " bytes (僅含 17 點去識別骨架)\n";
    std::cout << "  - RawRgbFrame:      " << sizeof(RawRgbFrame) << " bytes (嚴格隔離於 TEE RAM)\n\n";

    constexpr std::array<HardwareTier, 3> kTiers{
        HardwareTier::Phase1DistributedBox,
        HardwareTier::Tier2AllInOne,
        HardwareTier::Tier3Industrial};
    for (const HardwareTier t : kTiers) {
        const HardwareSpec& s = get_hardware_spec(t);
        std::cout << "  * " << s.tier_name << " | NPU: " << s.npu_tops_int8
                  << " TOPS | BW: " << s.memory_bandwidth_gbps << " GB/s"
                  << " | PoE 預算: " << s.poe_pd_budget_watts << " W"
                  << " | 雙軌 BOM: $" << s.bom_non_ndaa_min_usd << "-$" << s.bom_non_ndaa_max_usd
                  << " (NDAA: $" << s.bom_ndaa_min_usd << "-$" << s.bom_ndaa_max_usd << ")\n";
    }
}

void demonstrate_privacy_and_wipe(EdgeFusionCore& core) {
    print_separator();
    std::cout << "[2] Privacy-by-Design L0->L1->L2->L3 狀態機、Camera-Off 守衛與安全清零演練\n";
    core.reset();

    const std::uint64_t t0 = 1'000'000'000ULL;

    // Step A: L0 狀態下嘗試擷取 Raw RGB -> 必須被硬性拒絕
    RawRgbFrame secret_raw{};
    secret_raw.ptp_timestamp_ns = t0;
    secret_raw.sequence_id      = 1U;
    secret_raw.valid            = true;
    std::fill(secret_raw.pixels.begin(), secret_raw.pixels.end(), std::uint8_t{0xA5U});

    const RawCaptureGuardStatus guard_l0 = core.ingest_raw_rgb(secret_raw);
    std::cout << "  (L0 測試) 嘗試寫入 RawRgbFrame -> "
              << (guard_l0 == RawCaptureGuardStatus::RejectedCameraOffInL0L1 ? "已硬性拒絕 (Camera-Off 守衛生效)" : "異常放行")
              << " | 拒絕次數=" << core.fsm().snapshot(t0).rejected_raw_capture_count << "\n";

    // Step B: A1 衛浴跌倒觸發 L0 -> L1 -> L2
    CsiFrame csi{};
    csi.ptp_timestamp_ns = t0;
    csi.monotonic_rx_ns  = t0;
    csi.valid            = true;
    csi.fresnel_variance = 0.62F;
    csi.macro_motion_energy = 0.70F;
    core.push_csi(csi);

    MmWaveFrame mm{};
    mm.ptp_timestamp_ns         = t0 - 20'000'000ULL;  // 滯後 20ms，將由 100ms 視窗外推
    mm.monotonic_rx_ns          = t0;
    mm.valid                    = true;
    mm.point_count              = 1U;
    mm.target_x_m               = 1.20F;
    mm.target_y_m               = 2.10F;
    mm.target_z_m               = 0.30F;
    mm.vz_mps                   = -1.50F;
    mm.height_drop_rate_mps     = -1.80F;
    mm.micro_doppler_fall_score = 0.90F;
    mm.respiration_bpm          = 6.5F;  // 呼吸過緩
    core.push_mmwave(mm);

    ThermalFrame th{};
    th.ptp_timestamp_ns    = t0 - 10'000'000ULL;
    th.monotonic_rx_ns     = t0;
    th.valid               = true;
    th.max_temp_c          = 36.2F;
    th.human_thermal_score = 0.86F;
    core.push_thermal(th);

    const CoreStepInput in_a1{t0, ScenarioId::A1_ElderlyCare, false, 35.0F, 25.0F};
    const CoreStepOutput step1 = core.step(in_a1);  // L0 -> L1
    const CoreStepOutput step2 = core.step(in_a1);  // L1 -> L2

    std::cout << "  (階段轉移) Step1=" << level_to_str(step1.fsm_snapshot.level)
              << " -> Step2=" << level_to_str(step2.fsm_snapshot.level) << "\n";
    std::cout << "  (速度外推) 雷達滯後 20ms 外推補償: extrapolated="
              << (step2.aligned_slice.mmwave_extrapolated ? "true" : "false")
              << ", Z=" << std::fixed << std::setprecision(3) << step2.aligned_slice.mmwave.target_z_m
              << "m (原 0.300m, vz=-1.5m/s)\n";

    // Step C: L2 寫入 TEE Volatile RAM 並執行去識別化骨架推論 + <100ms 強制清零
    const RawCaptureGuardStatus guard_l2 = core.ingest_raw_rgb(secret_raw);
    RgbSkeletonFrame skel{};
    const bool wipe_ok = core.execute_l2_skeleton_and_wipe(skel, true, true, false, false);
    const WipeReport& wr = core.fsm().snapshot(t0).last_wipe_report;

    std::cout << "  (L2 RAM 推論+清零) 寫入狀態="
              << (guard_l2 == RawCaptureGuardStatus::AcceptedIntoTeeRam ? "AcceptedIntoTeeRam" : "Other")
              << " | 17點骨架跌倒=" << (skel.posture_fall_detected ? "true" : "false")
              << " | 清零驗證=" << (wipe_ok && wr.verified_all_zero ? "PASS (100% 全零)" : "FAIL")
              << " | 清零耗時=" << std::setprecision(2) << wr.duration_us << " us ("
              << wr.bytes_wiped << " bytes, <100ms SLA=" << (wr.within_sla_100ms ? "OK" : "NG") << ")\n";

    // Step D: 連續 3 幀高危確證觸發 L2 -> L3
    CoreStepOutput step_l3{};
    for (int i = 1; i <= 3; ++i) {
        const std::uint64_t ti = t0 + static_cast<std::uint64_t>(i) * 33'000'000ULL;
        csi.ptp_timestamp_ns   = ti;
        mm.ptp_timestamp_ns    = ti;
        th.ptp_timestamp_ns    = ti;
        skel.ptp_timestamp_ns  = ti;
        core.push_csi(csi);
        core.push_mmwave(mm);
        core.push_thermal(th);
        core.push_rgb_skeleton(skel);
        step_l3 = core.step(CoreStepInput{ti, ScenarioId::A1_ElderlyCare, false, 92.0F, 60.0F});
    }
    std::cout << "  (L3+降載保護) 連續 3 幀後狀態=" << level_to_str(step_l3.fsm_snapshot.level)
              << " | Graceful Degradation(NPU=92%)="
              << (step_l3.fsm_snapshot.graceful_degradation_active ? "啟動" : "關閉")
              << " (RGB=" << step_l3.fsm_snapshot.effective_rgb_fps << "fps, 雷達="
              << step_l3.fsm_snapshot.effective_mmwave_hz << "Hz)\n";
}

void demonstrate_five_scenarios(EdgeFusionCore& core) {
    print_separator();
    std::cout << "[3] 五大旗艦場景（A1, C4, B1, C1, B2）觸發與物理防誤報對照實測\n";

    struct CaseDesc {
        std::string_view label;
        ScenarioId       id;
        bool             is_true_positive;
    };

    constexpr std::array<CaseDesc, 10> kCases{{
        {"A1 真陽性: 衛浴跌倒 + 呼吸異常", ScenarioId::A1_ElderlyCare, true},
        {"A1 抗誤報: 低溫棉被掉落 / 寵物", ScenarioId::A1_ElderlyCare, false},
        {"C4 真陽性: BESS 熱失控 + 濃煙致盲", ScenarioId::C4_BessThermal, true},
        {"C4 抗誤報: EV 車輛煞車碟盤餘熱", ScenarioId::C4_BessThermal, false},
        {"B1 真陽性: 非白名單夜間入侵", ScenarioId::B1_Perimeter, true},
        {"B1 抗誤報: UWB 授權巡檢員白名單", ScenarioId::B1_Perimeter, false},
        {"C1 真陽性: 作業員逼近機械臂 (<0.8m)", ScenarioId::C1_HrcSafety, true},
        {"C1 抗誤報: 冷鏈水霧 / 垂掛晃動電纜", ScenarioId::C1_HrcSafety, false},
        {"B2 真陽性: 靜坐人員呼吸零 False-Off", ScenarioId::B2_SmartHvac, true},
        {"B2 抗誤報: 玻璃隔間外走廊路人", ScenarioId::B2_SmartHvac, false},
    }};

    std::uint64_t ts = 10'000'000'000ULL;
    for (const auto& c : kCases) {
        core.reset();
        ts += 1'000'000'000ULL;

        CsiFrame csi{};
        csi.ptp_timestamp_ns = ts;
        csi.monotonic_rx_ns  = ts;
        csi.valid            = true;

        MmWaveFrame mm{};
        mm.ptp_timestamp_ns = ts;
        mm.monotonic_rx_ns  = ts;
        mm.valid            = true;
        mm.point_count      = 2U;

        ThermalFrame th{};
        th.ptp_timestamp_ns = ts;
        th.monotonic_rx_ns  = ts;
        th.valid            = true;

        RgbSkeletonFrame rgb{};
        rgb.ptp_timestamp_ns = ts;
        rgb.monotonic_rx_ns  = ts;
        rgb.valid            = true;

        switch (c.id) {
            case ScenarioId::A1_ElderlyCare:
                csi.fresnel_variance = 0.60F;
                mm.target_z_m        = 0.25F;
                if (c.is_true_positive) {
                    mm.height_drop_rate_mps     = -1.75F;
                    mm.micro_doppler_fall_score = 0.92F;
                    mm.respiration_bpm          = 6.0F;
                    th.max_temp_c               = 36.4F;
                    th.human_thermal_score      = 0.88F;
                } else {
                    mm.height_drop_rate_mps     = -0.20F;
                    mm.micro_doppler_fall_score = 0.25F;
                    th.max_temp_c               = 25.5F;  // 冷棉被
                    th.human_thermal_score      = 0.12F;
                }
                core.push_csi(csi);
                core.push_mmwave(mm);
                core.push_thermal(th);
                break;

            case ScenarioId::C4_BessThermal:
                if (c.is_true_positive) {
                    th.max_temp_c               = 76.5F;
                    th.delta_temp_rate_c_per_s  = 0.35F;
                    mm.gait_human_likelihood    = 0.72F;  // 濃煙中有受困人員
                    mm.respiration_bpm          = 18.0F;
                    rgb.signal_quality          = 0.05F;  // 濃煙使光學攝影機致盲
                } else {
                    th.max_temp_c               = 64.0F;
                    th.delta_temp_rate_c_per_s  = -0.02F;
                    rgb.signal_quality          = 0.95F;
                    rgb.vehicle_heat_source_mask = true;  // 車輛煞車碟盤散熱
                    core.push_rgb_skeleton(rgb);
                }
                core.push_csi(csi);
                core.push_mmwave(mm);
                core.push_thermal(th);
                break;

            case ScenarioId::B1_Perimeter:
                csi.fresnel_variance = 0.58F;
                mm.radial_velocity_mps   = -1.35F;
                mm.gait_human_likelihood = 0.86F;
                th.max_temp_c            = 35.8F;
                th.human_thermal_score   = 0.85F;
                th.target_bbox_aspect_ratio = 0.45F;
                if (!c.is_true_positive) {
                    csi.whitelist_tag_present = true;
                    csi.whitelist_auth_level  = 2U;
                    csi.whitelist_distance_m  = 1.5F;
                }
                core.push_csi(csi);
                core.push_mmwave(mm);
                core.push_thermal(th);
                break;

            case ScenarioId::C1_HrcSafety:
                mm.target_x_m          = 0.55F;
                mm.target_y_m          = 0.45F;
                mm.target_z_m          = 0.30F;  // 距離 ~0.77m < 1.0m
                mm.radial_velocity_mps = -0.80F;
                if (c.is_true_positive) {
                    csi.whitelist_tag_present = true;
                    csi.whitelist_distance_m  = 0.75F;
                    th.max_temp_c             = 36.0F;
                    th.human_thermal_score    = 0.85F;
                } else {
                    th.max_temp_c             = 21.0F;  // 冷水霧或晃動電纜
                    th.human_thermal_score    = 0.10F;
                }
                core.push_csi(csi);
                core.push_mmwave(mm);
                core.push_thermal(th);
                break;

            case ScenarioId::B2_SmartHvac:
                if (c.is_true_positive) {
                    csi.fresnel_variance    = 0.08F;  // 靜坐打字低擾動
                    mm.respiration_bpm      = 15.5F;  // 呼吸微動偵測
                    th.max_temp_c           = 35.4F;
                    th.human_thermal_score  = 0.84F;
                    th.glass_blocked_hint   = 0.05F;
                } else {
                    csi.fresnel_variance     = 0.05F;
                    mm.radial_velocity_mps   = 1.20F;
                    mm.gait_human_likelihood = 0.80F;  // 雷達穿透玻璃測到走廊路人
                    th.max_temp_c            = 24.5F;
                    th.human_thermal_score   = 0.15F;
                    th.glass_blocked_hint    = 0.90F;  // LWIR 被普通玻璃阻隔
                }
                core.push_csi(csi);
                core.push_mmwave(mm);
                core.push_thermal(th);
                break;
        }

        const CoreStepOutput out = core.step(CoreStepInput{ts, c.id, false, 28.0F, 22.0F});
        std::cout << "  [" << out.evaluation.scenario_code << "] " << std::left << std::setw(36)
                  << c.label << " | Bel(Alert)=" << std::fixed << std::setprecision(3)
                  << out.ds_result.belief_alert
                  << " | 抑制=" << (out.evaluation.false_alarm_suppressed ? "YES" : "NO ")
                  << " | 聯動=" << actuator_to_str(out.evaluation.actuator_action) << "\n"
                  << "       摘要: " << out.evaluation.status_summary_zh
                  << " (抑制碼: " << suppression_to_str(out.evaluation.suppression_reason) << ")\n";
    }
}

void run_hot_path_benchmark(EdgeFusionCore& core) {
    print_separator();
    std::cout << "[4] 10,000 次零 Heap Allocation 熱路徑微基準測試\n";
    core.reset();

    constexpr std::size_t kIterations = 10'000U;
    std::array<std::uint64_t, kIterations> latencies_ns{};

    CsiFrame csi{};
    csi.valid            = true;
    csi.fresnel_variance = 0.42F;

    MmWaveFrame mm{};
    mm.valid                    = true;
    mm.point_count              = 4U;
    mm.target_x_m               = 1.5F;
    mm.target_y_m               = 1.2F;
    mm.target_z_m               = 0.4F;
    mm.vx_mps                   = 0.5F;
    mm.vy_mps                   = -0.4F;
    mm.vz_mps                   = -1.2F;
    mm.micro_doppler_fall_score = 0.80F;

    ThermalFrame th{};
    th.valid               = true;
    th.max_temp_c          = 36.5F;
    th.human_thermal_score = 0.85F;

    const auto bench_start = std::chrono::steady_clock::now();
    for (std::size_t i = 0U; i < kIterations; ++i) {
        const std::uint64_t t = 50'000'000'000ULL + static_cast<std::uint64_t>(i) * 33'333'333ULL;
        csi.ptp_timestamp_ns  = t;
        csi.monotonic_rx_ns   = t;
        mm.ptp_timestamp_ns   = t - 12'000'000ULL;
        mm.monotonic_rx_ns    = t;
        th.ptp_timestamp_ns   = t - 8'000'000ULL;
        th.monotonic_rx_ns    = t;

        const auto s0 = std::chrono::steady_clock::now();
        core.push_csi(csi);
        core.push_mmwave(mm);
        core.push_thermal(th);
        const volatile auto res =
            core.step(CoreStepInput{t, ScenarioId::A1_ElderlyCare, false, 45.0F, 30.0F});
        (void)res;
        const auto s1 = std::chrono::steady_clock::now();
        latencies_ns[i] = static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(s1 - s0).count());
    }
    const auto bench_end = std::chrono::steady_clock::now();

    std::sort(latencies_ns.begin(), latencies_ns.end());
    double sum_ns = 0.0;
    for (const auto ns : latencies_ns) {
        sum_ns += static_cast<double>(ns);
    }
    const double avg_us = (sum_ns / static_cast<double>(kIterations)) / 1000.0;
    const double p50_us = static_cast<double>(latencies_ns[kIterations / 2U]) / 1000.0;
    const double p99_us = static_cast<double>(latencies_ns[(kIterations * 99U) / 100U]) / 1000.0;
    const double total_ms =
        static_cast<double>(
            std::chrono::duration_cast<std::chrono::microseconds>(bench_end - bench_start).count()) /
        1000.0;

    std::cout << "  - 總迭代次數: " << kIterations << " | 總耗時: " << std::fixed
              << std::setprecision(2) << total_ms << " ms\n";
    std::cout << "  - 單步融合延遲: Avg=" << avg_us << " us | P50=" << p50_us
              << " us | P99=" << p99_us << " us (遠低於端側預算)\n";
    print_separator();
}

void pause_if_launched_from_explorer() noexcept {
#ifdef _WIN32
    DWORD pids[2]{};
    if (GetConsoleProcessList(pids, 2U) == 1U) {
        std::cout << "\n執行完成，按 Enter 鍵關閉視窗..." << std::flush;
        std::cin.clear();
        (void)std::cin.get();
    }
#endif
}

}  // namespace

int main() {
#ifdef _WIN32
    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);
#endif

    // 靜態配置 EdgeFusionCore（Zero Heap Allocation 且不佔用執行緒堆疊）
    static EdgeFusionCore g_edge_core{PrivacyFsmConfig{HardwareTier::Tier2AllInOne}};

    print_separator();
    std::cout << "四模態 AI 邊緣感測融合核心 (C++20 Embedded Edge Core v1.0)\n";
    std::cout << "ISAC 無線中樞 × Privacy-by-Design L0-L3 × 100ms 對齊 × Dempster-Shafer 引擎\n";

    print_hardware_table();
    demonstrate_privacy_and_wipe(g_edge_core);
    demonstrate_five_scenarios(g_edge_core);
    run_hot_path_benchmark(g_edge_core);
    pause_if_launched_from_explorer();

    return 0;
}
