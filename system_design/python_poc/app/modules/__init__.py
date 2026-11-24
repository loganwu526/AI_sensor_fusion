"""五大旗艦可插拔場景應用模組註冊工廠。"""

from __future__ import annotations

from ..models import ScenarioId
from .a1_elderly_care import A1ElderlyCareModule
from .b1_perimeter import B1PerimeterModule
from .b2_smart_hvac import B2SmartHvacModule
from .base import ScenarioModule
from .c1_hrc_safety import C1HrcSafetyModule
from .c4_bess_thermal import C4BessThermalModule


def create_scenario_registry() -> dict[ScenarioId, ScenarioModule]:
    """建立並回傳預設啟用的五大旗艦場景模組字典。"""
    modules: list[ScenarioModule] = [
        A1ElderlyCareModule(),
        C4BessThermalModule(),
        B1PerimeterModule(),
        C1HrcSafetyModule(),
        B2SmartHvacModule(),
    ]
    return {m.scenario_id: m for m in modules}


__all__ = [
    "ScenarioModule",
    "A1ElderlyCareModule",
    "C4BessThermalModule",
    "B1PerimeterModule",
    "C1HrcSafetyModule",
    "B2SmartHvacModule",
    "create_scenario_registry",
]
