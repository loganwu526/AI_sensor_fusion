#pragma once

/**
 * @file sensor_types.hpp
 * @brief 四模態邊緣感測融合核心之強型別與零動態配置感測幀定義
 *
 * 設計準則：
 * 1. 所有感測幀結構體皆為固定大小、可平凡複製（Trivially Copyable）與標準佈局（Standard Layout），
 *    確保關鍵熱路徑零動態記憶體配置（Zero Heap Allocation）並相容 DMA-BUF 零拷貝傳輸。
 * 2. 全面採用奈秒級 IEEE 1588 PTP 時間戳（ptp_timestamp_ns）與單調時鐘接收時間戳（monotonic_rx_ns）。
 * 3. RgbSkeletonFrame（僅含 17 點去識別化骨架）與 RawRgbFrame（僅限 TEE 揮發性 RAM）在型別層強制隔離。
 */

#include <array>
#include <cstddef>
#include <cstdint>
#include <string_view>
#include <type_traits>

namespace edge_fusion {

/// @brief 感測模態識別列舉
enum class ModalityId : std::uint8_t {
    WirelessCsi = 0,  ///< Wi-Fi 802.11bf CSI / BLE AoA / UWB
    MmWaveRadar = 1,  ///< 60GHz / 77GHz FMCW 4D 毫米波雷達
    ThermalIr   = 2,  ///< LWIR VOx 非製冷熱像儀
    RgbVision   = 3   ///< IP Camera 光學視覺（L2 去識別化骨架或 L3 受控存證）
};

/// @brief 三階硬體部署形態列舉（對齊研究報告第 8 章雙軌三階 BOM）
enum class HardwareTier : std::uint8_t {
    Phase1DistributedBox = 0,  ///< Phase 1 MVP：分散式利舊 Edge Box（RK3588 / QCS6490）+ 無鏡頭 Pod
    Tier2AllInOne        = 1,  ///< Phase 2 Tier 2：All-in-One 中高階商用智慧 AP（IPQ5424 + IWR6843AOP + Lepton 3.5）
    Tier3Industrial      = 2   ///< Tier 3 工業級：關鍵基礎設施寬溫閘道（Orin NX + AWR2944/雙60GHz + Seek/Boson）
};

/// @brief 五大旗艦融合應用場景列舉
enum class ScenarioId : std::uint8_t {
    A1_ElderlyCare = 0,  ///< #1 銀髮居家／長照衛浴零穿戴跌倒與呼吸守護
    C4_BessThermal = 1,  ///< #2 BESS 儲能／EV 超充熱失控 Stage 1 截斷與濃煙搜救
    B1_Perimeter   = 2,  ///< #3 關鍵園區／倉儲全天候零誤報周界入侵偵測
    C1_HrcSafety   = 3,  ///< #4 智慧工廠人機協作（HRC）／粉塵冷鏈盲區動態防撞
    B2_SmartHvac   = 4   ///< #5 智慧商辦微氣候 HVAC 零 False-Off 節能與夜間安防
};

/// @brief 編譯期常數定義
inline constexpr std::size_t kCsiSubcarriers        = 16U;
inline constexpr std::size_t kMaxMmWavePoints       = 32U;
inline constexpr std::size_t kThermalGridWidth      = 32U;
inline constexpr std::size_t kThermalGridHeight     = 24U;
inline constexpr std::size_t kThermalGridPixels     = kThermalGridWidth * kThermalGridHeight;  // 768 pixels (MLX90640 原生矩陣)
inline constexpr std::size_t kNumSkeletonKeypoints  = 17U;  // COCO 17 關鍵點
inline constexpr std::size_t kRawRgbWidth           = 64U;
inline constexpr std::size_t kRawRgbHeight          = 64U;
inline constexpr std::size_t kRawRgbChannels        = 4U;   // RGBA / XRGB 對齊 DMA 4-byte boundary
inline constexpr std::size_t kRawRgbPayloadBytes    = kRawRgbWidth * kRawRgbHeight * kRawRgbChannels;  // 16,384 bytes (16 KiB ROI 緩衝塊)

/// @brief 無線網路設備物理感知幀（IEEE 802.11bf CSI + BLE AoA / UWB 白名單融合摘要）
struct CsiFrame {
    std::uint64_t ptp_timestamp_ns{0ULL};
    std::uint64_t monotonic_rx_ns{0ULL};
    std::uint32_t sequence_id{0U};
    std::uint16_t channel_mhz{5180U};
    std::uint8_t  tx_antennas{2U};
    std::uint8_t  rx_antennas{2U};

    bool          valid{false};
    bool          whitelist_tag_present{false};    ///< BLE AoA / UWB 授權人員信標是否存在
    std::uint8_t  whitelist_auth_level{0U};        ///< 0: 無標籤, 1: 一般訪客, 2: 授權巡檢/作業員
    std::uint8_t  reserved_{0U};

    float         whitelist_distance_m{99.0F};     ///< UWB (10-30cm) 或 BLE AoA (0.3-1.0m) 測距結果
    float         fresnel_variance{0.0F};          ///< Fresnel 區跨子載波正規化擾動方差 [0, +inf)
    float         csi_ratio_phase_rad{0.0F};       ///< 雙天線 CSI Ratio 相位差（弧度）
    float         macro_motion_energy{0.0F};       ///< 宏觀空間動能指標 [0.0, 1.0]
    float         estimated_breath_bpm{0.0F};      ///< 802.11bf / CSI 粗估呼吸率 (breaths/min)
    float         signal_quality{1.0F};            ///< 訊號品質指標 [0.0, 1.0]

    std::array<float, kCsiSubcarriers> subcarrier_amplitudes{};  ///< 壓縮後之 16 子載波振幅包絡
};

/// @brief 單顆毫米波雷達 4D 點雲結構（3D 座標 + 徑向速度 + SNR + RCS）
struct MmWavePoint {
    float x_m{0.0F};
    float y_m{0.0F};
    float z_m{0.0F};
    float radial_velocity_mps{0.0F};
    float snr_db{0.0F};
    float rcs_dbsm{0.0F};
};

/// @brief 60GHz / 77GHz FMCW 毫米波雷達感測幀（100% 無視覺隱私，含 3D 速度供時間外推補償）
struct MmWaveFrame {
    std::uint64_t ptp_timestamp_ns{0ULL};
    std::uint64_t monotonic_rx_ns{0ULL};
    std::uint32_t sequence_id{0U};
    std::uint16_t point_count{0U};
    bool          valid{false};
    bool          in_lpds_sleep{false};  ///< 是否剛從 LPDS (~0.5mW) 喚醒之首幀

    float target_x_m{0.0F};              ///< 主追蹤目標 3D X 座標 (m)
    float target_y_m{0.0F};              ///< 主追蹤目標 3D Y 座標 (m)
    float target_z_m{0.0F};              ///< 主追蹤目標離地高度 Z (m)
    float vx_mps{0.0F};                  ///< 3D X 軸估測速度 (m/s)
    float vy_mps{0.0F};                  ///< 3D Y 軸估測速度 (m/s)
    float vz_mps{0.0F};                  ///< 3D Z 軸垂直速度 (m/s)
    float radial_velocity_mps{0.0F};     ///< 雷達徑向都卜勒速度 (m/s)，負值表示逼近感測器
    float height_drop_rate_mps{0.0F};    ///< 高度驟降率 dZ/dt (m/s)，負值大代表快速下墜
    float respiration_bpm{0.0F};         ///< 亞毫米胸腔微動呼吸率 (breaths/min)
    float heart_rate_bpm{0.0F};          ///< 心跳微動估測 (bpm)
    float micro_doppler_fall_score{0.0F};///< 跌倒微都卜勒特徵置信度 [0.0, 1.0]
    float gait_human_likelihood{0.0F};   ///< 人體步態識別置信度（排除樹枝/風扇擺動）[0.0, 1.0]
    float signal_quality{1.0F};          ///< 點雲品質與訊噪比評分 [0.0, 1.0]

    std::array<MmWavePoint, kMaxMmWavePoints> points{};
};

/// @brief LWIR VOx 非製冷熱像感測幀（含絕對輻射測溫、溫升斜率與熱飄移狀態）
struct ThermalFrame {
    std::uint64_t ptp_timestamp_ns{0ULL};
    std::uint64_t monotonic_rx_ns{0ULL};
    std::uint32_t sequence_id{0U};
    std::uint16_t width{static_cast<std::uint16_t>(kThermalGridWidth)};
    std::uint16_t height{static_cast<std::uint16_t>(kThermalGridHeight)};
    bool          valid{false};
    bool          ffc_in_progress{false};  ///< 是否正執行快門平場校正（FFC 650mW 突波期間）
    std::uint8_t  reserved_[2]{0U, 0U};

    float max_temp_c{25.0F};               ///< 場景最高輻射溫度 (°C)
    float min_temp_c{22.0F};               ///< 場景最低溫度 (°C)
    float ambient_temp_c{24.0F};           ///< 背景環境基準溫度 (°C)
    float fpa_temp_c{35.0F};               ///< 焦平面陣列溫度（供 ATC 飄移補償使用）
    float delta_temp_rate_c_per_s{0.0F};   ///< 熱點溫升斜率 ΔT/Δt (°C/s)
    float hotspot_x_norm{0.5F};            ///< 最熱質心正規化 X [0.0, 1.0]
    float hotspot_y_norm{0.5F};            ///< 最熱質心正規化 Y [0.0, 1.0]
    float target_bbox_aspect_ratio{0.5F};  ///< 熱斑寬高比（W/H，用於區分直立人形 vs 四足動物）
    float human_thermal_score{0.0F};       ///< 符合人體恒溫輪廓置信度 [0.0, 1.0]
    float glass_blocked_hint{0.0F};        ///< 玻璃隔間阻隔 LWIR 異常訊號指標 [0.0, 1.0]
    float signal_quality{1.0F};            ///< 感測有效品質（對比度過低或 FFC 時下降）[0.0, 1.0]

    /// @brief 32x24 輻射溫度陣列（單位：0.01 °C 定點整數，節省頻寬與零浮點解析開銷）
    std::array<std::int16_t, kThermalGridPixels> grid_centi_c{};
};

/// @brief 去識別化 2D 人體骨架關鍵點（僅含正規化座標與置信度，無任何像素資料）
struct Keypoint2D {
    float x_norm{0.0F};
    float y_norm{0.0F};
    float confidence{0.0F};
};

/// @brief L2 輸出之去識別化語意視覺幀（嚴格隔離：僅可輸出 17 點骨架、匿名框與火煙旗標）
struct RgbSkeletonFrame {
    std::uint64_t ptp_timestamp_ns{0ULL};
    std::uint64_t monotonic_rx_ns{0ULL};
    std::uint32_t sequence_id{0U};
    bool          valid{false};
    bool          person_detected{false};
    bool          flame_or_smoke_detected{false};
    bool          ppe_helmet_ok{true};
    bool          vehicle_heat_source_mask{false};  ///< 語意遮罩：標記為車輛引擎蓋/煞車碟盤等正常高溫部位
    bool          posture_fall_detected{false};
    bool          source_raw_wiped_verified{false}; ///< 來源 Raw RGB 緩衝區是否已確認全零清零
    std::uint8_t  reserved_{0U};

    std::uint64_t source_raw_wipe_ns{0ULL};         ///< 來源 Raw RGB 執行 secure_wipe() 之耗時（奈秒）
    float         semantic_alert_confidence{0.0F};  ///< 視覺模型語意風險置信度 [0.0, 1.0]
    float         bbox_x_norm{0.0F};
    float         bbox_y_norm{0.0F};
    float         bbox_w_norm{0.0F};
    float         bbox_h_norm{0.0F};
    float         signal_quality{1.0F};

    std::array<Keypoint2D, kNumSkeletonKeypoints> keypoints{};
};

/// @brief 嚴格隔離之原始 RGB 影格結構（僅限駐留於 TEE Volatile RAM 之 SecureRingBuffer）
/// @warning 系統禁止於 L0/L1 寫入此結構，且於 L2 提取 RgbSkeletonFrame 後必須立即執行 secure_wipe()
struct RawRgbFrame {
    std::uint64_t ptp_timestamp_ns{0ULL};
    std::uint64_t monotonic_rx_ns{0ULL};
    std::uint32_t sequence_id{0U};
    std::uint32_t exposure_us{8000U};
    std::uint16_t width{static_cast<std::uint16_t>(kRawRgbWidth)};
    std::uint16_t height{static_cast<std::uint16_t>(kRawRgbHeight)};
    std::uint8_t  channels{static_cast<std::uint8_t>(kRawRgbChannels)};
    bool          valid{false};
    std::uint8_t  reserved_[2]{0U, 0U};

    std::array<std::uint8_t, kRawRgbPayloadBytes> pixels{};
};

// 編譯期強制檢查所有感測幀皆為平凡可複製與標準佈局（POD 相容），確保可安全進行 DMA-BUF 與逐位元組安全覆寫
static_assert(std::is_trivially_copyable_v<CsiFrame>, "CsiFrame 必須為 trivially copyable");
static_assert(std::is_standard_layout_v<CsiFrame>, "CsiFrame 必須為 standard layout");
static_assert(std::is_trivially_copyable_v<MmWaveFrame>, "MmWaveFrame 必須為 trivially copyable");
static_assert(std::is_standard_layout_v<MmWaveFrame>, "MmWaveFrame 必須為 standard layout");
static_assert(std::is_trivially_copyable_v<ThermalFrame>, "ThermalFrame 必須為 trivially copyable");
static_assert(std::is_standard_layout_v<ThermalFrame>, "ThermalFrame 必須為 standard layout");
static_assert(std::is_trivially_copyable_v<RgbSkeletonFrame>, "RgbSkeletonFrame 必須為 trivially copyable");
static_assert(std::is_standard_layout_v<RgbSkeletonFrame>, "RgbSkeletonFrame 必須為 standard layout");
static_assert(std::is_trivially_copyable_v<RawRgbFrame>, "RawRgbFrame 必須為 trivially copyable");
static_assert(std::is_standard_layout_v<RawRgbFrame>, "RawRgbFrame 必須為 standard layout");

/// @brief 三階硬體規格配置表結構（零動態配置 constexpr 唯讀資料）
struct HardwareSpec {
    HardwareTier     tier;
    std::string_view tier_name;
    std::string_view wireless_soc;
    std::string_view npu_platform;
    std::string_view radar_chip;
    std::string_view thermal_chip;
    std::string_view camera_sensor;
    std::string_view poe_standard;

    float npu_tops_int8;
    float memory_bandwidth_gbps;
    float poe_pd_budget_watts;        ///< PD 可用直流功耗上限 (W)
    float ap_base_idle_watts;
    float ap_base_active_watts;
    float l0_incremental_watts;
    float l1_incremental_watts;
    float l2_incremental_watts;
    float l3_incremental_watts;

    float bom_non_ndaa_min_usd;
    float bom_non_ndaa_max_usd;
    float bom_ndaa_min_usd;
    float bom_ndaa_max_usd;

    std::uint16_t normal_rgb_fps;
    std::uint16_t degraded_rgb_fps;
    float         degradation_npu_threshold_pct;
};

/// @brief 三階硬體靜態唯讀配置表（C++20 編譯期常數，零動態配置）
inline constexpr std::array<HardwareSpec, 3> kHardwareSpecs{{
    // 1. Phase 1 MVP 分散式利舊融合閘道 + 無鏡頭感測 Pod
    HardwareSpec{
        HardwareTier::Phase1DistributedBox,
        "Phase 1 Distributed Edge Box",
        "ESP32-C6 / MediaTek Filogic 820 (Wi-Fi 6 + BLE/UWB)",
        "Rockchip RK3588 (6 TOPS) / Qualcomm QCS6490 (12-13 TOPS)",
        "TI IWRL6432 / IWR6843AOP (60GHz)",
        "Melexis MLX90640 (32x24) / FLIR Lepton 3.5 (160x120)",
        "Existing ONVIF/RTSP IP Camera (Brownfield)",
        "PoE+ 802.3at (25.5W PD) / 12V-24V DC",
        6.0F, 34.0F, 25.5F,
        6.0F, 11.0F,
        0.5F, 1.5F, 6.0F, 9.5F,
        141.0F, 174.0F, 165.0F, 210.0F,
        30U, 10U, 85.0F
    },
    // 2. Phase 2 Tier 2 中高階商用 All-in-One 智慧 AP 節點
    HardwareSpec{
        HardwareTier::Tier2AllInOne,
        "Tier 2 Commercial All-in-One AP",
        "Qualcomm IPQ5424 / MT7988 (Wi-Fi 7 2.5GbE)",
        "Qualcomm QCS6490 (12-13 TOPS) / Ambarella CV72 / RK3588",
        "TI IWR6843AOP (3T4R 60GHz) / Infineon BGT60TR13C",
        "FLIR Lepton 3.5 (160x120) / InfiRay Tiny1-C (256x192)",
        "Sony IMX415 / IMX678 STARVIS 2 (4K)",
        "PoE++ 802.3bt Type 3 (51.0W PD) / PoE+ Low-Power",
        12.0F, 51.0F, 51.0F,
        12.5F, 22.0F,
        0.6F, 2.0F, 7.5F, 12.0F,
        189.0F, 260.0F, 257.0F, 355.0F,
        30U, 10U, 85.0F
    },
    // 3. Tier 3 工業級 / 關鍵基礎設施節點
    HardwareSpec{
        HardwareTier::Tier3Industrial,
        "Tier 3 Industrial Critical Infra",
        "Qualcomm IPQ9574 + QCN9274 / Filogic 880 (Wi-Fi 7 10GbE)",
        "NVIDIA Jetson Orin NX (70-100 TOPS) / Ambarella CV5",
        "TI AWR2944 (77GHz Outdoor) / Dual 60GHz IWR6843 (Indoor)",
        "Seek Mosaic 320x240 / InfiRay 384 / FLIR Boson 640",
        "Sony IMX678 WDR IP67 Motorized Zoom",
        "PoE++ 802.3bt Type 4 (71.3W PD, 6kV Surge)",
        70.0F, 102.4F, 71.3F,
        18.0F, 28.0F,
        0.8F, 2.5F, 9.5F, 14.0F,
        649.0F, 960.0F, 2314.0F, 3695.0F,
        30U, 10U, 85.0F
    }
}};

/// @brief 取得指定硬體層級之編譯期規格配置表
[[nodiscard]] constexpr const HardwareSpec& get_hardware_spec(HardwareTier tier) noexcept {
    const auto idx = static_cast<std::size_t>(tier);
    return kHardwareSpecs[idx < kHardwareSpecs.size() ? idx : 1U];
}

}  // namespace edge_fusion
