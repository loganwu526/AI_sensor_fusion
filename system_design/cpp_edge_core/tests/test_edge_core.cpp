/**
 * @file test_edge_core.cpp
 * @brief C++20 嵌入式融合核心完整單元測試套件（零外部依賴 + Zero-Heap Audit）
 *
 * 測試覆蓋範圍：
 * 1. 型別與靜態規格表驗證（trivially_copyable / standard_layout / HardwareTier）。
 * 2. 熱路徑零動態記憶體配置稽核（透過全域 operator new 計數器實證 0 次 heap allocation）。
 * 3. SecureRingBuffer 溢位覆寫、DMA-BUF 零拷貝槽位、secure_wipe() 全零回讀與 <100ms SLA。
 * 4. 100ms 滑動視窗亂序排序、超時（>100ms）丟棄與雷達一階 3D/徑向速度外推數值精度。
 * 5. Dempster-Shafer 正交合併數值精度與 K >= 0.99 高衝突安全回退（防除零）。
 * 6. Privacy FSM (L0–L3) Camera-Off 守衛、L2 自動安全清零、30s 防抖遲滯與 Graceful Degradation。
 * 7. 五大旗艦場景（A1, C4, B1, C1, B2）真陽性觸發與領域抗誤報抑制測試。
 */

#include "ds_fusion.hpp"
#include "privacy_fsm.hpp"
#include "scenario_profiles.hpp"
#include "secure_ring_buffer.hpp"
#include "sensor_types.hpp"
#include "sliding_window.hpp"

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <iostream>
#include <new>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

// ============================================================================
// 全域 Heap Allocation 審計掛鉤（驗證關鍵熱路徑零動態記憶體配置）
// ============================================================================
namespace {
std::atomic<bool>          g_audit_heap_active{false};
std::atomic<std::uint64_t> g_heap_alloc_count{0ULL};
}  // namespace

void* operator new(std::size_t size) {
    if (g_audit_heap_active.load(std::memory_order_relaxed)) {
        g_heap_alloc_count.fetch_add(1ULL, std::memory_order_relaxed);
    }
    void* ptr = std::malloc(size == 0U ? 1U : size);
    if (ptr == nullptr) {
        throw std::bad_alloc();
    }
    return ptr;
}

void* operator new[](std::size_t size) {
    return ::operator new(size);
}

void operator delete(void* ptr) noexcept {
    std::free(ptr);
}

void operator delete(void* ptr, std::size_t) noexcept {
    std::free(ptr);
}

void operator delete[](void* ptr) noexcept {
    std::free(ptr);
}

void operator delete[](void* ptr, std::size_t) noexcept {
    std::free(ptr);
}

namespace {

using namespace edge_fusion;

int g_total_tests  = 0;
int g_failed_tests = 0;

#define CHECK_TRUE(expr)                                                                     \
    do {                                                                                     \
        if (!(expr)) {                                                                       \
            std::cerr << "  [FAIL] " << __FILE__ << ":" << __LINE__ << " -> " #expr << "\n"; \
            return false;                                                                    \
        }                                                                                    \
    } while (false)

#define CHECK_NEAR(actual, expected, tol)                                                     \
    do {                                                                                      \
        const double a_val_ = static_cast<double>(actual);                                    \
        const double e_val_ = static_cast<double>(expected);                                  \
        if (std::isnan(a_val_) || std::isinf(a_val_) || std::abs(a_val_ - e_val_) > (tol)) {  \
            std::cerr << "  [FAIL] " << __FILE__ << ":" << __LINE__ << " -> " #actual " ("    \
                      << a_val_ << ") != " << e_val_ << " ± " << (tol) << "\n";               \
            return false;                                                                     \
        }                                                                                     \
    } while (false)

using TestFn = bool (*)();

void run_test(const char* name, TestFn fn) {
    ++g_total_tests;
    const bool ok = fn();
    if (ok) {
        std::cout << "[PASS] " << name << "\n";
    } else {
        ++g_failed_tests;
        std::cout << "[FAIL] " << name << "\n";
    }
}

// 共用靜態核心執行個體（避免佔用測試執行緒堆疊）
EdgeFusionCore g_core{PrivacyFsmConfig{HardwareTier::Tier2AllInOne}};

// ----------------------------------------------------------------------------
// Test 1: 強型別 POD 與三階硬體規格表
// ----------------------------------------------------------------------------
bool test_sensor_types_and_hardware_table() {
    CHECK_TRUE(std::is_trivially_copyable_v<CsiFrame>);
    CHECK_TRUE(std::is_trivially_copyable_v<MmWaveFrame>);
    CHECK_TRUE(std::is_trivially_copyable_v<ThermalFrame>);
    CHECK_TRUE(std::is_trivially_copyable_v<RgbSkeletonFrame>);
    CHECK_TRUE(std::is_trivially_copyable_v<RawRgbFrame>);

    const auto& p1 = get_hardware_spec(HardwareTier::Phase1DistributedBox);
    const auto& t2 = get_hardware_spec(HardwareTier::Tier2AllInOne);
    const auto& t3 = get_hardware_spec(HardwareTier::Tier3Industrial);

    CHECK_NEAR(p1.npu_tops_int8, 6.0, 1e-6);
    CHECK_NEAR(t2.poe_pd_budget_watts, 51.0, 1e-6);
    CHECK_NEAR(t3.memory_bandwidth_gbps, 102.4, 1e-4);
    CHECK_TRUE(t2.bom_ndaa_max_usd > t2.bom_non_ndaa_max_usd);
    CHECK_TRUE(t3.bom_ndaa_min_usd >= 2000.0F);
    return true;
}

// ----------------------------------------------------------------------------
// Test 2: SecureRingBuffer 溢位覆寫、零拷貝 DMA-BUF 與 secure_wipe() 全零驗證
// ----------------------------------------------------------------------------
bool test_secure_ring_buffer_overflow_and_wipe() {
    static SecureRingBuffer<RawRgbFrame, 4U> ring;
    (void)ring.secure_wipe();
    CHECK_TRUE(ring.empty());
    CHECK_TRUE(ring.verify_all_zero());

    // 寫入 6 筆資料（容量 4，預期溢位 2 次，保留序號 3, 4, 5, 6）
    for (std::uint32_t seq = 1U; seq <= 6U; ++seq) {
        std::size_t slot = 0U;
        RawRgbFrame* raw = ring.acquire_write_slot(slot);
        CHECK_TRUE(raw != nullptr);
        raw->ptp_timestamp_ns = static_cast<std::uint64_t>(seq) * 1'000'000ULL;
        raw->sequence_id      = seq;
        raw->valid            = true;
        std::fill(raw->pixels.begin(), raw->pixels.end(), static_cast<std::uint8_t>(0x50U + seq));
        const bool overflowed = ring.commit_write_slot(slot);
        CHECK_TRUE(overflowed == (seq > 4U));
    }

    CHECK_TRUE(ring.full());
    CHECK_TRUE(ring.size() == 4U);
    CHECK_TRUE(ring.overflow_count() == 2U);
    CHECK_TRUE(ring.peek_latest(0U)->sequence_id == 6U);
    CHECK_TRUE(ring.peek_oldest(0U)->sequence_id == 3U);

    const DmaBufDescriptor dma = ring.export_dma_buf(0U);
    CHECK_TRUE(dma.valid);
    CHECK_TRUE(dma.byte_size == sizeof(RawRgbFrame));
    CHECK_TRUE(dma.ptp_timestamp_ns == 6'000'000ULL);

    // 執行強制安全清零並驗證 100% 全零且耗時遠低於 100 ms
    const WipeReport report = ring.secure_wipe();
    CHECK_TRUE(report.bytes_wiped == 4U * sizeof(RawRgbFrame));
    CHECK_TRUE(report.verified_all_zero);
    CHECK_TRUE(report.within_sla_100ms);
    CHECK_TRUE(report.duration_ns < 100'000'000ULL);
    CHECK_TRUE(ring.empty());
    CHECK_TRUE(ring.verify_all_zero());
    return true;
}

// ----------------------------------------------------------------------------
// Test 3: 100ms 滑動視窗對齊、亂序排序、超時丟棄與一階雷達速度外推
// ----------------------------------------------------------------------------
bool test_sliding_window_alignment_and_extrapolation() {
    SlidingWindowAligner aligner(kDefaultWindowNs);  // 100 ms

    // 亂序輸入多筆 CSI 與過期 Thermal 幀
    CsiFrame c_late{};
    c_late.ptp_timestamp_ns = 1'010'000'000ULL;
    c_late.monotonic_rx_ns  = 1'015'000'000ULL;
    c_late.sequence_id      = 2U;
    c_late.fresnel_variance = 0.60F;
    c_late.valid            = true;

    CsiFrame c_early{};
    c_early.ptp_timestamp_ns = 990'000'000ULL;
    c_early.monotonic_rx_ns  = 1'020'000'000ULL;  // 較晚抵達但 PTP 時間較早
    c_early.sequence_id      = 1U;
    c_early.fresnel_variance = 0.40F;
    c_early.valid            = true;

    CHECK_TRUE(aligner.push_csi(c_late));
    CHECK_TRUE(aligner.push_csi(c_early));

    // 雷達幀滯後目標時刻 40 ms（在 100 ms 視窗內），應觸發一階 3D 位置外推
    MmWaveFrame mm{};
    mm.ptp_timestamp_ns      = 960'000'000ULL;
    mm.monotonic_rx_ns       = 961'000'000ULL;
    mm.valid                 = true;
    mm.point_count           = 1U;
    mm.target_x_m            = 2.0F;
    mm.target_y_m            = 1.0F;
    mm.target_z_m            = 1.5F;
    mm.vx_mps                = 1.5F;
    mm.vy_mps                = -0.5F;
    mm.vz_mps                = -2.0F;
    mm.points[0]             = MmWavePoint{3.0F, 0.0F, 0.0F, -2.0F, 20.0F, 1.0F};
    CHECK_TRUE(aligner.push_mmwave(mm));

    // Thermal 幀超時（滯後目標時刻 130 ms > 100 ms），必須被視窗丟棄
    ThermalFrame th_stale{};
    th_stale.ptp_timestamp_ns = 870'000'000ULL;
    th_stale.monotonic_rx_ns  = 871'000'000ULL;
    th_stale.valid            = true;
    th_stale.max_temp_c       = 80.0F;
    CHECK_TRUE(aligner.push_thermal(th_stale));

    const AlignedMultiModalSlice slice = aligner.align(1'000'000'000ULL);
    CHECK_TRUE(slice.has_csi);
    CHECK_NEAR(slice.csi.fresnel_variance, 0.50, 1e-5);  // ±25ms 內 0.40 與 0.60 平均
    CHECK_TRUE(slice.csi_samples_aggregated == 2U);

    CHECK_TRUE(slice.has_mmwave);
    CHECK_TRUE(slice.mmwave_extrapolated);
    CHECK_NEAR(slice.mmwave_extrapolation_dt_ms, 40.0, 1e-6);
    // p(t) = p(t_r) + v * 0.04s => x=2.06, y=0.98, z=1.42; point[0].x = 3.0 + (-2.0)*0.04 = 2.92
    CHECK_NEAR(slice.mmwave.target_x_m, 2.06, 1e-5);
    CHECK_NEAR(slice.mmwave.target_y_m, 0.98, 1e-5);
    CHECK_NEAR(slice.mmwave.target_z_m, 1.42, 1e-5);
    CHECK_NEAR(slice.mmwave.points[0].x_m, 2.92, 1e-5);

    // 超時 >100ms 之熱像幀不得進入對齊切片
    CHECK_TRUE(!slice.has_thermal);
    CHECK_TRUE(slice.out_of_window_rejected >= 1ULL);

    // 測試無 3D 速度分量時之徑向速度外推回退
    SlidingWindowAligner radial_aligner(kDefaultWindowNs);
    MmWaveFrame mm_radial{};
    mm_radial.ptp_timestamp_ns    = 950'000'000ULL;  // dt = 50 ms
    mm_radial.valid               = true;
    mm_radial.target_x_m          = 3.0F;
    mm_radial.target_y_m          = 4.0F;  // R = 5.0 m, unit = (0.6, 0.8, 0)
    mm_radial.target_z_m          = 0.0F;
    mm_radial.radial_velocity_mps = -2.0F;
    radial_aligner.push_mmwave(mm_radial);
    const auto r_slice = radial_aligner.align(1'000'000'000ULL);
    // dr = -2.0 * 0.05 = -0.1 m => dx = -0.06, dy = -0.08 => (2.94, 3.92, 0.0)
    CHECK_NEAR(r_slice.mmwave.target_x_m, 2.94, 1e-5);
    CHECK_NEAR(r_slice.mmwave.target_y_m, 3.92, 1e-5);

    return true;
}

// ----------------------------------------------------------------------------
// Test 4: Dempster-Shafer 正交合併數值精度與 K >= 0.99 高衝突安全回退
// ----------------------------------------------------------------------------
bool test_dempster_shafer_math_and_conflict_fallback() {
    // 1. 手算精確驗證：m1 = {N:0.2, A:0.7, U:0.1}, m2 = {N:0.1, A:0.8, U:0.1}
    // K = 0.2*0.8 + 0.7*0.1 = 0.23 => 1 - K = 0.77
    // raw_N = 0.2*0.1 + 0.2*0.1 + 0.1*0.1 = 0.05 => m(N) = 0.05/0.77
    // raw_A = 0.7*0.8 + 0.7*0.1 + 0.1*0.8 = 0.71 => m(A) = 0.71/0.77
    // raw_U = 0.1*0.1 = 0.01                     => m(U) = 0.01/0.77
    double k_val      = 0.0;
    bool   high_confl = true;
    const MassFunction m1{0.2, 0.7, 0.1};
    const MassFunction m2{0.1, 0.8, 0.1};
    const MassFunction fused = DempsterShaferEngine::combine_pair(m1, m2, k_val, high_confl);

    CHECK_TRUE(!high_confl);
    CHECK_NEAR(k_val, 0.23, 1e-9);
    CHECK_NEAR(fused.normal, 0.05 / 0.77, 1e-9);
    CHECK_NEAR(fused.alert, 0.71 / 0.77, 1e-9);
    CHECK_NEAR(fused.uncertainty, 0.01 / 0.77, 1e-9);
    CHECK_NEAR(fused.sum(), 1.0, 1e-9);

    // 2. 極端高衝突測試：K >= 0.99 防除零回退並觸發升階旗標
    std::array<ModalityEvidence, kMaxDsSources> conflict_inputs{{
        {ModalityId::WirelessCsi, true, 1.0, {0.002, 0.996, 0.002}, {}},
        {ModalityId::MmWaveRadar, true, 1.0, {0.996, 0.002, 0.002}, {}},
        {ModalityId::ThermalIr, false, 0.0, {}, {}},
        {ModalityId::RgbVision, false, 0.0, {}, {}}
    }};
    const DsFusionResult ds_res = DempsterShaferEngine::combine_all(conflict_inputs);
    CHECK_TRUE(ds_res.high_conflict_escalation);
    CHECK_TRUE(ds_res.conflict_k >= 0.99);
    CHECK_NEAR(ds_res.fused_mass.sum(), 1.0, 1e-9);
    CHECK_TRUE(ds_res.fused_mass.uncertainty >= 0.99);
    return true;
}

// ----------------------------------------------------------------------------
// Test 5: Privacy FSM (Camera-Off 守衛、L2 自動清零、30s 防抖與降幀保護)
// ----------------------------------------------------------------------------
bool test_privacy_fsm_guards_hysteresis_and_degradation() {
    g_core.reset();
    const std::uint64_t t0 = 10'000'000'000ULL;

    // 1. L0 拒絕 Raw RGB 寫入
    RawRgbFrame raw{};
    raw.ptp_timestamp_ns = t0;
    raw.sequence_id      = 1U;
    raw.valid            = true;
    std::fill(raw.pixels.begin(), raw.pixels.end(), std::uint8_t{0x77U});

    CHECK_TRUE(g_core.fsm().current_level() == PrivacyLevel::L0_AmbientRf);
    CHECK_TRUE(g_core.ingest_raw_rgb(raw) == RawCaptureGuardStatus::RejectedCameraOffInL0L1);
    CHECK_TRUE(g_core.tee_raw_ring().empty());

    // 2. L0 -> L1 (依然拒絕 Raw RGB)
    CsiFrame csi{};
    csi.ptp_timestamp_ns = t0;
    csi.valid            = true;
    csi.fresnel_variance = 0.65F;

    MmWaveFrame mm{};
    mm.ptp_timestamp_ns         = t0;
    mm.valid                    = true;
    mm.target_z_m               = 0.25F;
    mm.height_drop_rate_mps     = -1.8F;
    mm.micro_doppler_fall_score = 0.90F;

    ThermalFrame th{};
    th.ptp_timestamp_ns    = t0;
    th.valid               = true;
    th.max_temp_c          = 36.5F;
    th.human_thermal_score = 0.85F;

    g_core.push_csi(csi);
    g_core.push_mmwave(mm);
    g_core.push_thermal(th);

    const auto s1 = g_core.step(CoreStepInput{t0, ScenarioId::A1_ElderlyCare, false, 30.0F, 30.0F});
    CHECK_TRUE(s1.fsm_snapshot.level == PrivacyLevel::L1_NonVisualVerify);
    CHECK_TRUE(g_core.ingest_raw_rgb(raw) == RawCaptureGuardStatus::RejectedCameraOffInL0L1);
    CHECK_TRUE(g_core.fsm().snapshot(t0).rejected_raw_capture_count == 2ULL);

    // 3. L1 -> L2：允許進 RAM，推論後自動執行 secure_wipe() 並驗證全零
    const auto s2 = g_core.step(CoreStepInput{t0 + 10'000'000ULL, ScenarioId::A1_ElderlyCare, false, 40.0F, 30.0F});
    CHECK_TRUE(s2.fsm_snapshot.level == PrivacyLevel::L2_DeidentifiedRam);
    CHECK_TRUE(g_core.ingest_raw_rgb(raw) == RawCaptureGuardStatus::AcceptedIntoTeeRam);

    RgbSkeletonFrame skel{};
    CHECK_TRUE(g_core.execute_l2_skeleton_and_wipe(skel, true, true, false));
    CHECK_TRUE(skel.source_raw_wiped_verified);
    CHECK_TRUE(skel.source_raw_wipe_ns < 100'000'000ULL);
    CHECK_TRUE(g_core.tee_raw_ring().verify_all_zero());

    // 4. Graceful Degradation 降幀保護 (NPU = 90% >= 85%)：RGB 降為 10 fps（每 3 幀收 1 幀）
    const auto s_deg = g_core.step(CoreStepInput{t0 + 20'000'000ULL, ScenarioId::A1_ElderlyCare, false, 90.0F, 40.0F});
    CHECK_TRUE(s_deg.fsm_snapshot.graceful_degradation_active);
    CHECK_TRUE(s_deg.fsm_snapshot.effective_rgb_fps == 10U);
    CHECK_TRUE(s_deg.fsm_snapshot.effective_mmwave_hz == 20U);

    int accepted = 0;
    int skipped  = 0;
    for (int f = 0; f < 6; ++f) {
        const auto st = g_core.ingest_raw_rgb(raw);
        if (st == RawCaptureGuardStatus::AcceptedIntoTeeRam) {
            ++accepted;
        } else if (st == RawCaptureGuardStatus::SkippedByGracefulDegradation) {
            ++skipped;
        }
    }
    CHECK_TRUE(accepted == 2 && skipped == 4);  // 30fps -> 10fps (1/3 採樣率)

    // 5. 30 秒遲滯防抖測試（冷卻 20s 不可降階，滿 30s 才可降回 L1 且自動清零 RAM）
    const std::uint64_t t_calm = t0 + 1'000'000'000ULL;
    csi.ptp_timestamp_ns        = t_calm;
    csi.fresnel_variance        = 0.05F;
    mm.ptp_timestamp_ns         = t_calm;
    mm.target_z_m               = 1.65F;
    mm.height_drop_rate_mps     = 0.0F;
    mm.micro_doppler_fall_score = 0.05F;
    mm.respiration_bpm          = 15.0F;
    th.ptp_timestamp_ns         = t_calm;
    skel.ptp_timestamp_ns       = t_calm;
    skel.posture_fall_detected  = false;
    g_core.push_csi(csi);
    g_core.push_mmwave(mm);
    g_core.push_thermal(th);
    g_core.push_rgb_skeleton(skel);

    // 進入冷卻起點
    const auto c0 = g_core.step(CoreStepInput{t_calm, ScenarioId::A1_ElderlyCare, false, 30.0F, 20.0F});
    CHECK_TRUE(c0.fsm_snapshot.level == PrivacyLevel::L2_DeidentifiedRam);
    CHECK_TRUE(c0.fsm_snapshot.hysteresis_cooldown_active);

    // 僅過 20 秒：不得降階
    const std::uint64_t t_20s = t_calm + 20'000'000'000ULL;
    csi.ptp_timestamp_ns = t_20s;
    mm.ptp_timestamp_ns  = t_20s;
    th.ptp_timestamp_ns  = t_20s;
    skel.ptp_timestamp_ns = t_20s;
    g_core.push_csi(csi);
    g_core.push_mmwave(mm);
    g_core.push_thermal(th);
    g_core.push_rgb_skeleton(skel);
    const auto c20 = g_core.step(CoreStepInput{t_20s, ScenarioId::A1_ElderlyCare, false, 30.0F, 20.0F});
    CHECK_TRUE(c20.fsm_snapshot.level == PrivacyLevel::L2_DeidentifiedRam);

    // 滿 30 秒：安全降回 L1 並確保 TEE RAM 全零
    const std::uint64_t t_30s = t_calm + 30'000'000'000ULL;
    csi.ptp_timestamp_ns  = t_30s;
    mm.ptp_timestamp_ns   = t_30s;
    th.ptp_timestamp_ns   = t_30s;
    skel.ptp_timestamp_ns = t_30s;
    g_core.push_csi(csi);
    g_core.push_mmwave(mm);
    g_core.push_thermal(th);
    g_core.push_rgb_skeleton(skel);
    const auto c30 = g_core.step(CoreStepInput{t_30s, ScenarioId::A1_ElderlyCare, false, 30.0F, 20.0F});
    CHECK_TRUE(c30.fsm_snapshot.level == PrivacyLevel::L1_NonVisualVerify);
    CHECK_TRUE(g_core.tee_raw_ring().verify_all_zero());
    return true;
}

// ----------------------------------------------------------------------------
// Test 6: 五大場景（A1, C4, B1, C1, B2）觸發與抗誤報完整驗證
// ----------------------------------------------------------------------------
bool test_five_scenarios_trigger_and_suppression() {
    const std::uint64_t ts = 20'000'000'000ULL;

    // A1: 衛浴水蒸氣誤報抑制 vs 真實跌倒觸發
    {
        g_core.reset();
        CsiFrame csi{};
        csi.ptp_timestamp_ns = ts;
        csi.valid            = true;
        csi.fresnel_variance = 0.55F;
        ThermalFrame th{};
        th.ptp_timestamp_ns    = ts;
        th.valid               = true;
        th.max_temp_c          = 38.0F;
        th.human_thermal_score = 0.20F;  // 彌漫水蒸氣無人體輪廓
        MmWaveFrame mm{};
        mm.ptp_timestamp_ns         = ts;
        mm.valid                    = true;
        mm.micro_doppler_fall_score = 0.20F;
        g_core.push_csi(csi);
        g_core.push_thermal(th);
        g_core.push_mmwave(mm);
        const auto res = g_core.step(CoreStepInput{ts, ScenarioId::A1_ElderlyCare, false, 20.0F, 20.0F});
        CHECK_TRUE(res.evaluation.false_alarm_suppressed);
        CHECK_TRUE(res.evaluation.suppression_reason == SuppressionReason::WaterVaporFiltered);
        CHECK_TRUE(!res.evaluation.alert_triggered);
    }

    // C4: 熱失控 Stage 1 + 濃煙致盲仍觸發 BMS 急停 vs 車輛煞車餘熱過濾
    {
        g_core.reset();
        ThermalFrame th{};
        th.ptp_timestamp_ns        = ts;
        th.valid                   = true;
        th.max_temp_c              = 74.0F;
        th.delta_temp_rate_c_per_s = 0.30F;
        MmWaveFrame mm{};
        mm.ptp_timestamp_ns      = ts;
        mm.valid                 = true;
        mm.point_count           = 2U;
        mm.gait_human_likelihood = 0.70F;
        RgbSkeletonFrame rgb{};
        rgb.ptp_timestamp_ns = ts;
        rgb.valid            = true;
        rgb.signal_quality   = 0.05F;  // 濃煙全盲
        g_core.push_thermal(th);
        g_core.push_mmwave(mm);
        g_core.push_rgb_skeleton(rgb);
        const auto crit = g_core.step(CoreStepInput{ts, ScenarioId::C4_BessThermal, false, 25.0F, 20.0F});
        CHECK_TRUE(crit.evaluation.alert_triggered);
        CHECK_TRUE(crit.evaluation.actuator_action == ActuatorCommand::BmsEmergencyTrip);

        // 對照：煞車碟盤餘熱
        g_core.reset();
        th.max_temp_c                = 65.0F;
        th.delta_temp_rate_c_per_s   = -0.01F;
        rgb.signal_quality           = 0.95F;
        rgb.vehicle_heat_source_mask = true;
        g_core.push_thermal(th);
        g_core.push_rgb_skeleton(rgb);
        const auto sup = g_core.step(CoreStepInput{ts, ScenarioId::C4_BessThermal, false, 25.0F, 20.0F});
        CHECK_TRUE(sup.evaluation.false_alarm_suppressed);
        CHECK_TRUE(sup.evaluation.suppression_reason ==
                   SuppressionReason::VehicleExhaustOrSunGlintFiltered);
    }

    // B1: 四足動物誤報抑制 vs 真實入侵觸發
    {
        g_core.reset();
        MmWaveFrame mm{};
        mm.ptp_timestamp_ns      = ts;
        mm.valid                 = true;
        mm.target_z_m            = 0.40F;
        mm.gait_human_likelihood = 0.25F;
        ThermalFrame th{};
        th.ptp_timestamp_ns         = ts;
        th.valid                    = true;
        th.max_temp_c               = 36.0F;
        th.target_bbox_aspect_ratio = 1.60F;  // 寬大於高（犬隻/狐狸）
        g_core.push_mmwave(mm);
        g_core.push_thermal(th);
        const auto sup = g_core.step(CoreStepInput{ts, ScenarioId::B1_Perimeter, false, 20.0F, 20.0F});
        CHECK_TRUE(sup.evaluation.false_alarm_suppressed);
        CHECK_TRUE(sup.evaluation.suppression_reason ==
                   SuppressionReason::SwayingTreeOrAnimalFiltered);
    }

    // C1: 1.8m 降速 / 0.7m E-Stop / 晃動電纜過濾
    {
        g_core.reset();
        MmWaveFrame mm{};
        mm.ptp_timestamp_ns    = ts;
        mm.valid               = true;
        mm.target_x_m          = 1.8F;
        mm.radial_velocity_mps = -0.6F;
        ThermalFrame th{};
        th.ptp_timestamp_ns    = ts;
        th.valid               = true;
        th.max_temp_c          = 35.8F;
        th.human_thermal_score = 0.85F;
        CsiFrame csi{};
        csi.ptp_timestamp_ns      = ts;
        csi.valid                 = true;
        csi.whitelist_tag_present = true;
        csi.whitelist_distance_m  = 1.8F;
        g_core.push_mmwave(mm);
        g_core.push_thermal(th);
        g_core.push_csi(csi);
        const auto slow = g_core.step(CoreStepInput{ts, ScenarioId::C1_HrcSafety, false, 20.0F, 20.0F});
        CHECK_TRUE(slow.evaluation.actuator_action == ActuatorCommand::PlcSafeSlowdown);

        // 水霧 / 低溫垂掛電纜過濾
        g_core.reset();
        mm.target_x_m             = 0.8F;
        th.max_temp_c             = 22.0F;
        th.human_thermal_score    = 0.15F;
        csi.whitelist_tag_present = false;
        g_core.push_mmwave(mm);
        g_core.push_thermal(th);
        g_core.push_csi(csi);
        const auto sup = g_core.step(CoreStepInput{ts, ScenarioId::C1_HrcSafety, false, 20.0F, 20.0F});
        CHECK_TRUE(sup.evaluation.false_alarm_suppressed);
        CHECK_TRUE(sup.evaluation.suppression_reason ==
                   SuppressionReason::WaterMistOrCableSwingFiltered);
    }

    // B2: 靜坐呼吸零 False-Off vs 玻璃外行人過濾
    {
        g_core.reset();
        CsiFrame csi{};
        csi.ptp_timestamp_ns = ts;
        csi.valid            = true;
        csi.fresnel_variance = 0.05F;
        MmWaveFrame mm{};
        mm.ptp_timestamp_ns = ts;
        mm.valid            = true;
        mm.respiration_bpm  = 14.0F;
        ThermalFrame th{};
        th.ptp_timestamp_ns    = ts;
        th.valid               = true;
        th.max_temp_c          = 35.0F;
        th.human_thermal_score = 0.80F;
        th.glass_blocked_hint  = 0.0F;
        g_core.push_csi(csi);
        g_core.push_mmwave(mm);
        g_core.push_thermal(th);
        const auto hold = g_core.step(CoreStepInput{ts, ScenarioId::B2_SmartHvac, false, 15.0F, 15.0F});
        CHECK_TRUE(hold.evaluation.actuator_action == ActuatorCommand::HvacComfortHold);

        // 玻璃外行人排除
        g_core.reset();
        mm.respiration_bpm       = 0.0F;
        mm.radial_velocity_mps   = 1.1F;
        mm.gait_human_likelihood = 0.78F;
        th.human_thermal_score   = 0.15F;
        th.glass_blocked_hint    = 0.85F;
        g_core.push_csi(csi);
        g_core.push_mmwave(mm);
        g_core.push_thermal(th);
        const auto eco = g_core.step(CoreStepInput{ts, ScenarioId::B2_SmartHvac, false, 15.0F, 15.0F});
        CHECK_TRUE(eco.evaluation.false_alarm_suppressed);
        CHECK_TRUE(eco.evaluation.suppression_reason ==
                   SuppressionReason::GlassOutsidePasserbyFiltered);
        CHECK_TRUE(eco.evaluation.actuator_action == ActuatorCommand::HvacEcoSetback);
    }

    return true;
}

// ----------------------------------------------------------------------------
// Test 7: 熱路徑零動態記憶體配置（Zero Heap Allocation）審計測試
// ----------------------------------------------------------------------------
bool test_zero_heap_allocation_hot_path() {
    g_core.reset();
    const std::uint64_t ts = 30'000'000'000ULL;

    CsiFrame csi{};
    csi.ptp_timestamp_ns = ts;
    csi.valid            = true;
    csi.fresnel_variance = 0.70F;

    MmWaveFrame mm{};
    mm.ptp_timestamp_ns         = ts;
    mm.valid                    = true;
    mm.target_z_m               = 0.3F;
    mm.vx_mps                   = 0.5F;
    mm.height_drop_rate_mps     = -1.5F;
    mm.micro_doppler_fall_score = 0.85F;

    ThermalFrame th{};
    th.ptp_timestamp_ns    = ts;
    th.valid               = true;
    th.max_temp_c          = 36.2F;
    th.human_thermal_score = 0.85F;

    RawRgbFrame raw{};
    raw.ptp_timestamp_ns = ts;
    raw.valid            = true;
    std::fill(raw.pixels.begin(), raw.pixels.end(), std::uint8_t{0x33U});

    // 啟動 Heap Allocation 攔截審計
    g_heap_alloc_count.store(0ULL, std::memory_order_seq_cst);
    g_audit_heap_active.store(true, std::memory_order_seq_cst);

    for (std::uint64_t i = 0ULL; i < 100ULL; ++i) {
        const std::uint64_t cur = ts + i * 33'000'000ULL;
        csi.ptp_timestamp_ns    = cur;
        mm.ptp_timestamp_ns     = cur;
        th.ptp_timestamp_ns     = cur;
        raw.ptp_timestamp_ns    = cur;

        (void)g_core.push_csi(csi);
        (void)g_core.push_mmwave(mm);
        (void)g_core.push_thermal(th);
        const auto out = g_core.step(CoreStepInput{cur, ScenarioId::A1_ElderlyCare, false, 50.0F, 40.0F});
        if (out.fsm_snapshot.level == PrivacyLevel::L2_DeidentifiedRam) {
            (void)g_core.ingest_raw_rgb(raw);
            RgbSkeletonFrame skel{};
            (void)g_core.execute_l2_skeleton_and_wipe(skel, true, true, false);
        }
    }

    g_audit_heap_active.store(false, std::memory_order_seq_cst);
    const std::uint64_t allocs = g_heap_alloc_count.load(std::memory_order_seq_cst);
    CHECK_TRUE(allocs == 0ULL);
    return true;
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

    std::cout << "=== 執行 C++20 嵌入式邊緣融合核心單元測試 ===\n";

    run_test("1. SensorTypes POD 與三階硬體規格表", test_sensor_types_and_hardware_table);
    run_test("2. SecureRingBuffer 溢位、DMA-BUF 與 <100ms 全零安全清零",
             test_secure_ring_buffer_overflow_and_wipe);
    run_test("3. 100ms 滑動視窗排序、超時丟棄與雷達一階速度外推",
             test_sliding_window_alignment_and_extrapolation);
    run_test("4. Dempster-Shafer 正交合併精度與 K>=0.99 高衝突安全回退",
             test_dempster_shafer_math_and_conflict_fallback);
    run_test("5. Privacy FSM Camera-Off 守衛、L2 清零、30s 防抖與降幀保護",
             test_privacy_fsm_guards_hysteresis_and_degradation);
    run_test("6. 五大旗艦場景 (A1/C4/B1/C1/B2) 觸發與領域抗誤報抑制",
             test_five_scenarios_trigger_and_suppression);
    run_test("7. 關鍵熱路徑 Zero Heap Allocation 審計 (0 次 malloc/new)",
             test_zero_heap_allocation_hot_path);

    std::cout << "=== 測試彙總：總計 " << g_total_tests << " 項，通過 "
              << (g_total_tests - g_failed_tests) << " 項，失敗 " << g_failed_tests << " 項 ===\n";

    pause_if_launched_from_explorer();

    return (g_failed_tests == 0) ? 0 : 1;
}
