#pragma once

/**
 * @file ds_fusion.hpp
 * @brief 無 Heap Allocation 之 Dempster-Shafer (D-S) 證據理論多模態融合引擎
 *
 * 辨識框架（Frame of Discernment）Θ = {Normal, Alert}：
 * - 基本機率分配（BPA / Mass Function）：m(Normal), m(Alert), m(Uncertainty = Θ)
 *   滿足 m(Normal) + m(Alert) + m(Uncertainty) = 1.0，且各項 >= 0.0。
 * - Dempster 正交合併與衝突係數 K：
 *   K = m1(Normal)*m2(Alert) + m1(Alert)*m2(Normal)
 * - 高衝突安全防護（K >= 0.99）：
 *   絕對防止 1 / (1 - K) 除零或數值溢位，自動回退至 Yager 安全保守合併規則
 *   （將衝突質量轉入 Uncertainty Θ），並標記 high_conflict_escalation = true 通知狀態機升階仲裁。
 */

#include "sensor_types.hpp"

#include <array>
#include <cstddef>

namespace edge_fusion {

inline constexpr std::size_t kMaxDsSources          = 4U;
inline constexpr double      kHighConflictThreshold = 0.99;

/// @brief 單一證據源或融合後之基本機率分配（BPA）
struct MassFunction {
    double normal{0.0};
    double alert{0.0};
    double uncertainty{1.0};

    /// @brief 鉗位負值並正規化確保總和嚴格為 1.0
    void sanitize_and_normalize() noexcept;

    [[nodiscard]] double sum() const noexcept { return normal + alert + uncertainty; }
};

/// @brief 單一模態之原始與可靠度折現後證據輸入
struct ModalityEvidence {
    ModalityId   source{ModalityId::WirelessCsi};
    bool         present{false};
    double       reliability_alpha{1.0};  ///< Shafer 可靠度折現權重 α ∈ [0.0, 1.0]
    MassFunction raw_bpa{};
    MassFunction discounted_bpa{};
};

/// @brief D-S 證據合併完整輸出結果
struct DsFusionResult {
    MassFunction fused_mass{};
    double       belief_alert{0.0};         ///< Bel(Alert) = m(Alert)
    double       plausibility_alert{1.0};   ///< Pl(Alert)  = m(Alert) + m(Uncertainty)
    double       belief_normal{0.0};        ///< Bel(Normal) = m(Normal)
    double       plausibility_normal{1.0};  ///< Pl(Normal)  = m(Normal) + m(Uncertainty)

    double       conflict_k{0.0};           ///< 合併過程最大單步或等效衝突係數 K ∈ [0.0, 1.0]
    double       cumulative_conflict{0.0};  ///< 累積總衝突度量 1 - ∏(1 - K_i)
    bool         high_conflict_escalation{false};  ///< 若任一步驟 K >= 0.99 則為 true（啟動升階仲裁）
    std::size_t  active_source_count{0U};

    std::array<ModalityEvidence, kMaxDsSources> evaluated_sources{};
};

/// @brief 零 Heap Allocation Dempster-Shafer 證據融合引擎
class DempsterShaferEngine {
public:
    /// @brief 對單一模態 BPA 套用 Shafer 可靠度折現因子 α ∈ [0.0, 1.0]
    /// @details m^α(N) = α·m(N), m^α(A) = α·m(A), m^α(Θ) = (1 - α) + α·m(Θ)
    [[nodiscard]] static MassFunction discount_bpa(const MassFunction& bpa,
                                                   double reliability_alpha) noexcept;

    /// @brief 合併兩組 MassFunction，並於 K >= 0.99 時啟用安全回退防止除零
    [[nodiscard]] static MassFunction combine_pair(const MassFunction& m1,
                                                   const MassFunction& m2,
                                                   double& out_conflict_k,
                                                   bool& out_high_conflict) noexcept;

    /// @brief 以固定大小陣列執行多達四模態之循序正交合併
    [[nodiscard]] static DsFusionResult combine_all(
        const std::array<ModalityEvidence, kMaxDsSources>& inputs) noexcept;
};

}  // namespace edge_fusion
