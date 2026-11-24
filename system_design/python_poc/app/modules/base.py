"""可插拔場景應用模組抽象基類（ScenarioModule）。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import AlignedFrame, DsFusionResult, ScenarioEvaluation, ScenarioId


class ScenarioModule(ABC):
    """五大旗艦融合應用場景之統一插件介面基類。"""

    @property
    @abstractmethod
    def scenario_id(self) -> ScenarioId:
        """回傳場景識別碼（A1 / C4 / B1 / C1 / B2）。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """回傳場景繁體中文名稱。"""

    @property
    @abstractmethod
    def description(self) -> str:
        """回傳場景技術目標與防誤報機制簡述。"""

    @property
    @abstractmethod
    def sensor_weights(self) -> dict[str, float]:
        """回傳四模態於本場景的權重配置（供儀表與規則參考）。"""

    @abstractmethod
    def evaluate(
        self,
        aligned_frame: AlignedFrame,
        ds_result: DsFusionResult,
    ) -> ScenarioEvaluation:
        """根據 100ms 對齊後的多模態幀與 D-S 融合結果評估場景風險與聯動指令。"""
