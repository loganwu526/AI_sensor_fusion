#pragma once

/**
 * @file secure_ring_buffer.hpp
 * @brief 零拷貝安全環狀緩衝區（模擬 DMA-BUF 語意）與防 DSE 最佳化之強制記憶體清零機制
 *
 * 核心安全保證：
 * 1. 固定模板容量 SecureRingBuffer<T, N>，完全無動態 Heap 配置。
 * 2. 支援無鎖單生產者/零拷貝寫入槽位取得（acquire_write_slot / commit_write_slot）與 DMA-BUF 描述子匯出。
 * 3. secure_wipe() 使用 volatile std::uint8_t* 逐位元組覆寫整個 backing buffer，
 *    並搭配 std::atomic_signal_fence(std::memory_order_seq_cst) 徹底阻斷編譯器
 *    Dead-Store Elimination (DSE) 最佳化移除清零動作，同時以 std::chrono::steady_clock
 *    精確量測清零耗時與進行全零回讀驗證（目標遠小於 100 ms）。
 */

#include <array>
#include <atomic>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <type_traits>

namespace edge_fusion {

/// @brief 模擬 Linux DMA-BUF 零拷貝共享緩衝區描述子
struct DmaBufDescriptor {
    std::size_t   slot_index{0U};
    std::uintptr_t sim_phys_addr{0U};
    std::size_t   byte_size{0U};
    std::uint64_t ptp_timestamp_ns{0ULL};
    bool          valid{false};
};

/// @brief 強制記憶體清零結果報告（供 L2 隱私稽核與 <100 ms SLA 驗證）
struct WipeReport {
    std::size_t   bytes_wiped{0U};
    std::uint64_t duration_ns{0ULL};
    double        duration_us{0.0};
    double        duration_ms{0.0};
    bool          verified_all_zero{false};
    bool          within_sla_100ms{false};
};

/// @brief 固定容量零拷貝安全環狀緩衝區
/// @tparam T 感測幀型別（必須為 trivially copyable，避免解構子殘留或非平坦記憶體）
/// @tparam N 環狀緩衝槽位容量（須 >= 2）
template <typename T, std::size_t N>
class SecureRingBuffer {
    static_assert(N >= 2U, "SecureRingBuffer 容量 N 必須大於或等於 2");
    static_assert(std::is_trivially_copyable_v<T>,
                  "SecureRingBuffer 元素型別 T 必須為 trivially copyable 才能執行安全位元組清零");

public:
    static constexpr std::size_t   kCapacity     = N;
    static constexpr std::uint64_t kWipeSlaNs    = 100'000'000ULL;  // 100 ms 強制隱私清零紅線

    SecureRingBuffer() noexcept = default;

    // 禁止意外拷貝大塊安全緩衝區（避免敏感影像意外殘留於堆疊副本）
    SecureRingBuffer(const SecureRingBuffer&) = delete;
    SecureRingBuffer& operator=(const SecureRingBuffer&) = delete;

    [[nodiscard]] constexpr std::size_t capacity() const noexcept { return N; }

    [[nodiscard]] std::size_t size() const noexcept {
        return count_.load(std::memory_order_acquire);
    }

    [[nodiscard]] bool empty() const noexcept {
        return size() == 0U;
    }

    [[nodiscard]] bool full() const noexcept {
        return size() == N;
    }

    [[nodiscard]] std::size_t overflow_count() const noexcept {
        return overflow_count_.load(std::memory_order_relaxed);
    }

    [[nodiscard]] std::uint64_t total_writes() const noexcept {
        return total_writes_.load(std::memory_order_relaxed);
    }

    /// @brief 零拷貝寫入第一步：取得下一個可直接由 DMA/驅動寫入的實體槽位指標
    /// @param out_slot_index 回傳取得的槽位索引，供後續 commit_write_slot() 使用
    /// @return 指向緩衝區槽位之指標
    [[nodiscard]] T* acquire_write_slot(std::size_t& out_slot_index) noexcept {
        const std::size_t head = head_.load(std::memory_order_relaxed);
        out_slot_index         = head;
        // 若槽位已有舊資料且即將覆寫，先將其清零確保無殘留位元組混合
        if (slot_populated_[head]) {
            wipe_memory_region(reinterpret_cast<volatile std::uint8_t*>(
                                   static_cast<void*>(&buffer_[head])),
                               sizeof(T));
        }
        return &buffer_[head];
    }

    /// @brief 零拷貝寫入第二步：提交已完成 DMA 填充之槽位，並於滿載時推進 tail（溢位覆蓋最舊幀）
    /// @param slot_index 由 acquire_write_slot() 取得之索引
    /// @return 若發生環狀溢位覆寫最舊資料則回傳 true，否則回傳 false
    bool commit_write_slot(std::size_t slot_index) noexcept {
        const std::size_t expected_head = head_.load(std::memory_order_relaxed);
        if (slot_index != expected_head) {
            return false;
        }

        slot_populated_[slot_index] = true;
        const std::size_t next_head = advance_index(expected_head);
        head_.store(next_head, std::memory_order_release);
        total_writes_.fetch_add(1ULL, std::memory_order_relaxed);

        const std::size_t current_count = count_.load(std::memory_order_relaxed);
        if (current_count == N) {
            // 環狀緩衝已滿：推進最舊資料指標（Pre-roll 覆蓋策略）並累計溢位次數
            const std::size_t tail = tail_.load(std::memory_order_relaxed);
            tail_.store(advance_index(tail), std::memory_order_release);
            overflow_count_.fetch_add(1ULL, std::memory_order_relaxed);
            return true;
        }

        count_.store(current_count + 1U, std::memory_order_release);
        return false;
    }

    /// @brief 便利寫入介面（內部仍為無 heap 配置之原地寫入）
    /// @return 若發生環狀溢位覆寫最舊資料回傳 true，正常寫入未溢位回傳 false
    bool push(const T& frame) noexcept {
        std::size_t slot = 0U;
        T* dst           = acquire_write_slot(slot);
        *dst             = frame;
        return commit_write_slot(slot);
    }

    /// @brief 依反向偏移量取得零拷貝唯讀指標（0 = 最新一幀，1 = 前一幀）
    [[nodiscard]] const T* peek_latest(std::size_t reverse_offset = 0U) const noexcept {
        const std::size_t current_count = count_.load(std::memory_order_acquire);
        if (reverse_offset >= current_count) {
            return nullptr;
        }
        const std::size_t head = head_.load(std::memory_order_acquire);
        const std::size_t idx  = retreat_index(head, reverse_offset + 1U);
        return &buffer_[idx];
    }

    /// @brief 依正向偏移量取得最舊起算之唯讀指標（0 = 目前保留中最舊一幀）
    [[nodiscard]] const T* peek_oldest(std::size_t forward_offset = 0U) const noexcept {
        const std::size_t current_count = count_.load(std::memory_order_acquire);
        if (forward_offset >= current_count) {
            return nullptr;
        }
        const std::size_t tail = tail_.load(std::memory_order_acquire);
        const std::size_t idx  = (tail + forward_offset) % N;
        return &buffer_[idx];
    }

    /// @brief 匯出指定最新偏移槽位之模擬 DMA-BUF 描述子（零拷貝供 NPU / VPU 讀取）
    [[nodiscard]] DmaBufDescriptor export_dma_buf(std::size_t reverse_offset = 0U) const noexcept {
        const std::size_t current_count = count_.load(std::memory_order_acquire);
        if (reverse_offset >= current_count) {
            return DmaBufDescriptor{};
        }
        const std::size_t head = head_.load(std::memory_order_acquire);
        const std::size_t idx  = retreat_index(head, reverse_offset + 1U);

        DmaBufDescriptor desc{};
        desc.slot_index    = idx;
        desc.sim_phys_addr = reinterpret_cast<std::uintptr_t>(&buffer_[idx]);
        desc.byte_size     = sizeof(T);
        desc.valid         = true;
        if constexpr (requires(const T& item) { item.ptp_timestamp_ns; }) {
            desc.ptp_timestamp_ns = buffer_[idx].ptp_timestamp_ns;
        }
        return desc;
    }

    /// @brief 單一槽位強制安全清零
    WipeReport secure_wipe_slot(std::size_t slot_index) noexcept {
        WipeReport report{};
        if (slot_index >= N) {
            return report;
        }

        const auto t0 = std::chrono::steady_clock::now();
        volatile std::uint8_t* ptr = reinterpret_cast<volatile std::uint8_t*>(
            static_cast<void*>(&buffer_[slot_index]));
        wipe_memory_region(ptr, sizeof(T));
        std::atomic_signal_fence(std::memory_order_seq_cst);
        std::atomic_thread_fence(std::memory_order_seq_cst);

        const bool all_zero = verify_region_zero(ptr, sizeof(T));
        const auto t1       = std::chrono::steady_clock::now();

        slot_populated_[slot_index] = false;
        const auto elapsed_ns = static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count());

        report.bytes_wiped       = sizeof(T);
        report.duration_ns       = elapsed_ns;
        report.duration_us       = static_cast<double>(elapsed_ns) / 1000.0;
        report.duration_ms       = static_cast<double>(elapsed_ns) / 1'000'000.0;
        report.verified_all_zero = all_zero;
        report.within_sla_100ms  = all_zero && (elapsed_ns < kWipeSlaNs);
        return report;
    }

    /// @brief 強制將整個環狀緩衝區（包含結構體對齊填充位元組）逐位元組覆寫清零
    /// @details 採用 volatile std::uint8_t* 覆寫並於寫入後加上 atomic_signal_fence，
    ///          保證在高階編譯最佳化（-O2 / -O3）下絕對不會被 Dead-Store Elimination 消除。
    WipeReport secure_wipe() noexcept {
        const auto t0 = std::chrono::steady_clock::now();

        volatile std::uint8_t* raw_ptr = reinterpret_cast<volatile std::uint8_t*>(
            static_cast<void*>(buffer_.data()));
        const std::size_t total_bytes = sizeof(buffer_);

        wipe_memory_region(raw_ptr, total_bytes);

        // 雙重記憶體柵欄：防止編譯器 DSE 刪除迴圈，並確保硬體匯流排觀察到寫入完成
        std::atomic_signal_fence(std::memory_order_seq_cst);
        std::atomic_thread_fence(std::memory_order_seq_cst);

        head_.store(0U, std::memory_order_release);
        tail_.store(0U, std::memory_order_release);
        count_.store(0U, std::memory_order_release);
        for (std::size_t i = 0U; i < N; ++i) {
            slot_populated_[i] = false;
        }

        const bool all_zero = verify_region_zero(raw_ptr, total_bytes);
        const auto t1       = std::chrono::steady_clock::now();

        const auto elapsed_ns = static_cast<std::uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(t1 - t0).count());

        WipeReport report{};
        report.bytes_wiped       = total_bytes;
        report.duration_ns       = elapsed_ns;
        report.duration_us       = static_cast<double>(elapsed_ns) / 1000.0;
        report.duration_ms       = static_cast<double>(elapsed_ns) / 1'000'000.0;
        report.verified_all_zero = all_zero;
        report.within_sla_100ms  = all_zero && (elapsed_ns < kWipeSlaNs);
        last_wipe_report_        = report;
        return report;
    }

    /// @brief 獨立驗證整個環狀緩衝區位元組是否皆為 0x00（透過 volatile 讀取避免編譯器假設）
    [[nodiscard]] bool verify_all_zero() const noexcept {
        volatile const std::uint8_t* raw_ptr = reinterpret_cast<volatile const std::uint8_t*>(
            static_cast<const void*>(buffer_.data()));
        return verify_region_zero(raw_ptr, sizeof(buffer_));
    }

    /// @brief 取得原始記憶體位元組大小
    [[nodiscard]] constexpr std::size_t raw_byte_capacity() const noexcept {
        return sizeof(buffer_);
    }

    [[nodiscard]] const WipeReport& last_wipe_report() const noexcept {
        return last_wipe_report_;
    }

private:
    static constexpr std::size_t advance_index(std::size_t idx) noexcept {
        if constexpr ((N & (N - 1U)) == 0U) {
            return (idx + 1U) & (N - 1U);
        } else {
            return (idx + 1U) % N;
        }
    }

    static constexpr std::size_t retreat_index(std::size_t idx, std::size_t step) noexcept {
        return (idx + N - (step % N)) % N;
    }

    static void wipe_memory_region(volatile std::uint8_t* ptr, std::size_t bytes) noexcept {
        for (std::size_t i = 0U; i < bytes; ++i) {
            ptr[i] = 0x00U;
        }
        std::atomic_signal_fence(std::memory_order_seq_cst);
    }

    static bool verify_region_zero(volatile const std::uint8_t* ptr, std::size_t bytes) noexcept {
        std::uint8_t accumulator = 0U;
        for (std::size_t i = 0U; i < bytes; ++i) {
            accumulator |= ptr[i];
        }
        std::atomic_signal_fence(std::memory_order_seq_cst);
        return accumulator == 0U;
    }

    alignas(64) std::array<T, N> buffer_{};
    std::array<bool, N>          slot_populated_{};
    std::atomic<std::size_t>     head_{0U};
    std::atomic<std::size_t>     tail_{0U};
    std::atomic<std::size_t>     count_{0U};
    std::atomic<std::size_t>     overflow_count_{0U};
    std::atomic<std::uint64_t>   total_writes_{0ULL};
    WipeReport                   last_wipe_report_{};
};

}  // namespace edge_fusion
