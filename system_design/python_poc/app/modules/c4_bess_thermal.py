"""場景模組 C4：BESS 鋰電池儲能站／EV 超充站熱失控 Stage 1 截斷與濃煙搜救。"""

from __future__ import annotations

from ..models import (
    AlignedFrame,
    AntiFalseAlarmFlag,
    DsFusionResult,
    PrivacyLevel,
    RiskLevel,
    ScenarioEvaluation,
    ScenarioId,
)
from .base import ScenarioModule


class C4BessThermalModule(ScenarioModule):
    """C4 儲能與高壓設施早期溫升斜率截斷＋濃煙穿透雷達受困搜尋模組。"""

    @property
    def scenario_id(self) -> ScenarioId:
        return ScenarioId.C4

    @property
    def name(self) -> str:
        return "C4 BESS 熱失控 Stage 1 截斷與濃煙搜救"

    @property
    def description(self) -> str:
        return "偵測電池機櫃溫升斜率（ΔT/Δt）於冒煙前切斷 BMS 電源，並於濃煙零視距下以 mmWave 定位受困人員。"

    @property
    def sensor_weights(self) -> dict[str, float]:
        return {"csi": 0.10, "mmwave": 0.30, "thermal": 0.45, "rgb": 0.15}

    def evaluate(
        self,
        aligned_frame: AlignedFrame,
        ds_result: DsFusionResult,
    ) -> ScenarioEvaluation:
        mmw = aligned_frame.mmwave
        thm = aligned_frame.thermal
        rgb = aligned_frame.rgb

        flags: list[AntiFalseAlarmFlag] = []
        reasons: list[str] = []
        commands: list[dict] = []

        # 防誤報規則：排除汽車煞車碟盤餘熱／深色車頂太陽直射或反射熱斑
        vehicle_glare_filtered = False
        if (rgb is not None and rgb.vehicle_or_glare_heat_mask) or (
            thm is not None and thm.is_glass_boundary_reflection and thm.temp_rise_rate_c_per_min < 2.0
        ):
            vehicle_glare_filtered = True
        flags.append(
            AntiFalseAlarmFlag(
                rule_name="VEHICLE_BRAKE_SOLAR_GLARE_FILTER",
                triggered=vehicle_glare_filtered,
                description="透過視覺部位語意遮罩與溫升斜率排除車輛煞車餘熱及太陽反光",
            )
        )

        if vehicle_glare_filtered:
            reasons.append("已識別高溫來源為車輛煞車餘熱或陽光反射，抑制誤觸發急停")
            return ScenarioEvaluation(
                scenario_id=self.scenario_id,
                scenario_name=self.name,
                risk_level=RiskLevel.SAFE,
                triggered=False,
                false_alarm_suppressed=True,
                trigger_reasons=reasons,
                anti_false_alarm_flags=flags,
                northbound_commands=[],
                recommended_privacy_level=PrivacyLevel.L1,
                confidence=0.88,
            )

        stage1_thermal = thm is not None and (
            thm.max_temp_c >= 65.0 or thm.temp_rise_rate_c_per_min >= 5.0
        )
        smoke_rescue_needed = mmw is not None and mmw.smoke_penetration_active and len(mmw.points) > 0
        visual_smoke_flame = rgb is not None and rgb.smoke_or_flame_visual

        if stage1_thermal or smoke_rescue_needed or visual_smoke_flame:
            if thm is not None and stage1_thermal:
                reasons.append(
                    f"Thermal 偵測 Stage 1 電芯／接點異常升溫 ({thm.max_temp_c:.1f}°C, 斜率 +{thm.temp_rise_rate_c_per_min:.1f}°C/min)"
                )
            if smoke_rescue_needed and mmw is not None:
                reasons.append(
                    f"濃煙零視距下 mmWave 成功穿透並鎖定現場人員 3D 座標 ({len(mmw.points)} 點雲, 距離 {mmw.target_distance_m:.1f}m)"
                )
            if visual_smoke_flame:
                reasons.append("視覺引擎確認煙霧／早期電弧閃光特徵")

            is_critical = (
                (thm is not None and thm.max_temp_c >= 80.0)
                or smoke_rescue_needed
                or visual_smoke_flame
            )
            risk = RiskLevel.CRITICAL if is_critical else RiskLevel.HIGH
            rec_level = PrivacyLevel.L3 if is_critical else PrivacyLevel.L2

            commands.append(
                {
                    "protocol": "Modbus TCP / 乾接點繼電器",
                    "target": "BESS BMS 主斷路器與 EV 超充樁急停迴路",
                    "action": "STAGE1_BMS_EMERGENCY_CUTOFF",
                    "payload": {
                        "max_temp_c": thm.max_temp_c if thm else 0.0,
                        "temp_rise_rate_c_per_min": thm.temp_rise_rate_c_per_min if thm else 0.0,
                        "person_trapped_in_smoke": smoke_rescue_needed,
                        "radar_rescue_pos": aligned_frame.extrapolated_position,
                    },
                }
            )
            if smoke_rescue_needed:
                commands.append(
                    {
                        "protocol": "Wi-Fi 7 Mesh / 5G URLLC",
                        "target": "消防救災指揮車終端",
                        "action": "DISPATCH_SMOKE_PENETRATION_RADAR_POS",
                        "payload": {"trapped_distance_m": mmw.target_distance_m if mmw else 0.0},
                    }
                )

            return ScenarioEvaluation(
                scenario_id=self.scenario_id,
                scenario_name=self.name,
                risk_level=risk,
                triggered=True,
                false_alarm_suppressed=False,
                trigger_reasons=reasons,
                anti_false_alarm_flags=flags,
                northbound_commands=commands,
                recommended_privacy_level=rec_level,
                confidence=round(max(ds_result.belief_alert, 0.85 if is_critical else 0.72), 4),
            )

        # 一般電池櫃無異常監測
        if aligned_frame.csi is not None and aligned_frame.csi.fresnel_variance > 0.25:
            reasons.append("維運人員巡檢中，儲能櫃溫度正常")
            rec = PrivacyLevel.L1
        else:
            reasons.append("儲能櫃溫度與環境均正常，維持常駐低功耗巡檢")
            rec = PrivacyLevel.L0
        return ScenarioEvaluation(
            scenario_id=self.scenario_id,
            scenario_name=self.name,
            risk_level=RiskLevel.SAFE,
            triggered=False,
            false_alarm_suppressed=False,
            trigger_reasons=reasons,
            anti_false_alarm_flags=flags,
            northbound_commands=[],
            recommended_privacy_level=rec,
            confidence=round(max(ds_result.belief_normal, 0.82), 4),
        )
