#pragma once

/**
 * @file sliding_window.hpp
 * @brief 100 ms 滑動視窗非同步四模態時空對齊器（零 Heap 配置 + 雷達一階速度外推補償）
 *
 * 核心工程機制：
 * 1. 非同步亂序接收：各模態緩衝區依 (ptp_timestamp_ns, monotonic_rx_ns) 嚴格遞增排序，
 *    容許網路與序列埠驅動（100-1000Hz CSI / 10-20Hz mmWave / 9-30Hz Thermal / 30fps RGB）之亂序到達。
 * 2. 100 ms 滑動視窗（100,000,000 ns）：鎖定最接近目標時間戳 target_ptp_ns 之多模態切片，
 *    超過 100 ms 時間容差之過期幀自動判定為超時並丟棄。
 * 3. 一階運動外推補償：當毫米波雷達最新幀落後目標時刻時（例如 10 Hz 雷達對齊 30 fps 視覺時刻），
 *    利用雷達 3D 速度向量 (vx, vy, vz) 或徑向都卜勒速度執行一階位置外推補償：
 *    p_hat(t) = p(t_r) + v * (t - t_r)。
 */

#include "sensor_types.hpp"

#include <array>
#include <cstddef>
#include <cstdint>

namespace edge_fusion {

inline constexpr std::uint64_t kDefaultWindowNs       = 100'000'000ULL;  // 100 ms 對齊視窗
inline constexpr std::uint64_t kStrictSyncToleranceNs = 15'000'000ULL;   // 15 ms 嚴格對齊誤差參考
inline constexpr std::uint64_t kCsiSubWindowNs        = 25'000'000ULL;   // ±25 ms 高頻 CSI 短時窗聚合

/// @brief 固定容量有序滑動佇列（無動態記憶體配置）
template <typename FrameT, std::size_t Capacity>
class OrderedWindowQueue {
    static_assert(Capacity >= 2U, "OrderedWindowQueue 容量必須 >= 2");

public:
    OrderedWindowQueue() noexcept = default;

    [[nodiscard]] std::size_t size() const noexcept { return count_; }
    [[nodiscard]] constexpr std::size_t capacity() const noexcept { return Capacity; }
    [[nodiscard]] bool empty() const noexcept { return count_ == 0U; }
    [[nodiscard]] std::uint64_t dropped_stale_count() const noexcept { return dropped_stale_; }

    void clear() noexcept {
        count_         = 0U;
        dropped_stale_ = 0ULL;
    }

    /// @brief 依 (ptp_timestamp_ns, monotonic_rx_ns) 升序插入感測幀；若滿載則淘汰最舊幀
    /// @return true 代表成功納管，false 代表無效幀或時間戳比現有最舊視窗更早而遭丟棄
    bool insert(const FrameT& frame) noexcept {
        if (!frame.valid || frame.ptp_timestamp_ns == 0ULL) {
            ++dropped_stale_;
            return false;
        }

        // 若緩衝已滿且新幀甚至比現存最舊幀還舊，直接視為過期丟棄
        if (count_ == Capacity && is_before(frame, items_[0])) {
            ++dropped_stale_;
            return false;
        }

        // 若已滿，先騰出最舊一格（索引 0 左移淘汰）
        if (count_ == Capacity) {
            for (std::size_t i = 1U; i < Capacity; ++i) {
                items_[i - 1U] = items_[i];
            }
            --count_;
            ++dropped_stale_;
        }

        // 尋找升序插入位置（通常新幀落在尾端，從尾端掃描極快）
        std::size_t pos = count_;
        while (pos > 0U && is_before(frame, items_[pos - 1U])) {
            items_[pos] = items_[pos - 1U];
            --pos;
        }
        items_[pos] = frame;
        ++count_;
        return true;
    }

    /// @brief 淘汰所有嚴格小於 cutoff_ptp_ns 的過期幀
    std::size_t prune_older_than(std::uint64_t cutoff_ptp_ns) noexcept {
        std::size_t remove_count = 0U;
        while (remove_count < count_ && items_[remove_count].ptp_timestamp_ns < cutoff_ptp_ns) {
            ++remove_count;
        }
        if (remove_count > 0U) {
            for (std::size_t i = remove_count; i < count_; ++i) {
                items_[i - remove_count] = items_[i];
            }
            count_ -= remove_count;
            dropped_stale_ += remove_count;
        }
        return remove_count;
    }

    /// @brief 在 [target_ptp_ns - max_window_ns, target_ptp_ns + max_window_ns] 內尋找最接近目標時間戳之幀
    [[nodiscard]] const FrameT* find_closest(std::uint64_t target_ptp_ns,
                                             std::uint64_t max_window_ns,
                                             std::int64_t& out_skew_ns) const noexcept {
        const FrameT* best      = nullptr;
        std::uint64_t best_diff = UINT64_MAX;
        std::int64_t  best_skew = 0LL;

        for (std::size_t i = 0U; i < count_; ++i) {
            const std::uint64_t ts = items_[i].ptp_timestamp_ns;
            const std::uint64_t diff =
                (target_ptp_ns >= ts) ? (target_ptp_ns - ts) : (ts - target_ptp_ns);
            if (diff <= max_window_ns && diff < best_diff) {
                best_diff = diff;
                best      = &items_[i];
                best_skew = (target_ptp_ns >= ts)
                                ? static_cast<std::int64_t>(diff)
                                : -static_cast<std::int64_t>(diff);
            }
        }
        out_skew_ns = best_skew;
        return best;
    }

    [[nodiscard]] const FrameT& at(std::size_t idx) const noexcept { return items_[idx]; }

private:
    static bool is_before(const FrameT& a, const FrameT& b) noexcept {
        if (a.ptp_timestamp_ns != b.ptp_timestamp_ns) {
            return a.ptp_timestamp_ns < b.ptp_timestamp_ns;
        }
        return a.monotonic_rx_ns < b.monotonic_rx_ns;
    }

    std::array<FrameT, Capacity> items_{};
    std::size_t                  count_{0U};
    std::uint64_t                dropped_stale_{0ULL};
};

/// @brief 100 ms 滑動視窗輸出之四模態對齊切片
struct AlignedMultiModalSlice {
    std::uint64_t target_ptp_ns{0ULL};

    bool has_csi{false};
    bool has_mmwave{false};
    bool has_thermal{false};
    bool has_rgb_skeleton{false};

    /// @brief 與目標時間戳之時間偏差（target - frame_ts，正值代表感測幀早於目標時刻）
    std::int64_t csi_skew_ns{0LL};
    std::int64_t mmwave_skew_ns{0LL};
    std::int64_t thermal_skew_ns{0LL};
    std::int64_t rgb_skew_ns{0LL};

    bool          within_15ms_strict_sync{false};
    bool          mmwave_extrapolated{false};
    double        mmwave_extrapolation_dt_ms{0.0};
    std::size_t   csi_samples_aggregated{0U};
    std::uint64_t out_of_window_rejected{0ULL};

    CsiFrame         csi{};
    MmWaveFrame      mmwave{};
    ThermalFrame     thermal{};
    RgbSkeletonFrame rgb_skeleton{};
};

/// @brief 非同步四模態 100 ms 滑動視窗對齊器
class SlidingWindowAligner {
public:
    static constexpr std::size_t kCsiQueueCap     = 32U;
    static constexpr std::size_t kMmWaveQueueCap  = 16U;
    static constexpr std::size_t kThermalQueueCap = 16U;
    static constexpr std::size_t kRgbQueueCap     = 16U;

    explicit SlidingWindowAligner(std::uint64_t window_ns = kDefaultWindowNs) noexcept
        : window_ns_(window_ns) {}

    void reset() noexcept;

    bool push_csi(const CsiFrame& frame) noexcept;
    bool push_mmwave(const MmWaveFrame& frame) noexcept;
    bool push_thermal(const ThermalFrame& frame) noexcept;
    bool push_rgb_skeleton(const RgbSkeletonFrame& frame) noexcept;

    /// @brief 淘汰早於 (anchor_ptp_ns - retention_span_ns) 之過期歷史幀
    std::size_t prune_expired(std::uint64_t anchor_ptp_ns) noexcept;

    /// @brief 對齊最接近 target_ptp_ns 且位於 100 ms 視窗內之四模態切片（含雷達一階速度外推）
    [[nodiscard]] AlignedMultiModalSlice align(std::uint64_t target_ptp_ns) const noexcept;

    [[nodiscard]] std::uint64_t window_ns() const noexcept { return window_ns_; }
    [[nodiscard]] std::uint64_t total_dropped_stale() const noexcept;

    [[nodiscard]] std::size_t csi_buffered() const noexcept { return csi_queue_.size(); }
    [[nodiscard]] std::size_t mmwave_buffered() const noexcept { return mmwave_queue_.size(); }
    [[nodiscard]] std::size_t thermal_buffered() const noexcept { return thermal_queue_.size(); }
    [[nodiscard]] std::size_t rgb_buffered() const noexcept { return rgb_queue_.size(); }

private:
    void track_latest_ptp(std::uint64_t ptp_ns) noexcept;

    std::uint64_t window_ns_{kDefaultWindowNs};
    std::uint64_t latest_seen_ptp_ns_{0ULL};

    OrderedWindowQueue<CsiFrame, kCsiQueueCap>         csi_queue_{};
    OrderedWindowQueue<MmWaveFrame, kMmWaveQueueCap>   mmwave_queue_{};
    OrderedWindowQueue<ThermalFrame, kThermalQueueCap> thermal_queue_{};
    OrderedWindowQueue<RgbSkeletonFrame, kRgbQueueCap> rgb_queue_{};
};

}  // namespace edge_fusion
