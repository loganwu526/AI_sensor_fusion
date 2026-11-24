"""場景模組 C1：智慧工廠人機協作（HRC）與倉儲粉塵冷鏈盲區動態防撞系統。"""

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


class C1HrcSafetyModule(ScenarioModule):
    """C1 ISO/TS 15066 動態防撞＋UWB 跨貨架預警＋晃動電纜與水霧過濾模組。"""

    @property
    def scenario_id(self) -> ScenarioId:
        return ScenarioId.C1

    @property
    def name(self) -> str:
        return "C1 HRC 人機防撞與粉塵水霧誤急停過濾"

    @property
    def description(self) -> str:
        return "融合 UWB 跨貨架盲區測距、60GHz 防塵逼近速度向量與 Thermal 活體人體過濾，防止電纜晃動與高壓水霧導致產線誤急停。"

    @property
    def sensor_weights(self) -> dict[str, float]:
        return {"csi": 0.25, "mmwave": 0.40, "thermal": 0.20, "rgb": 0.15}

    def evaluate(
        self,
        aligned_frame: AlignedFrame,
        ds_result: DsFusionResult,
    ) -> ScenarioEvaluation:
        csi = aligned_frame.csi
        mmw = aligned_frame.mmwave
        thm = aligned_frame.thermal
        rgb = aligned_frame.rgb

        flags: list[AntiFalseAlarmFlag] = []
        reasons: list[str] = []
        commands: list[dict] = []

        # 防誤報規則：晃動高架電纜、塑膠條形門簾、高壓清洗水霧／蒸汽過濾
        no_uwb_danger = csi is None or (
            not csi.uwb_nlos_alert
            and (csi.uwb_tag_distance_m is None or csi.uwb_tag_distance_m > 2.5)
        )
        non_human_clutter = mmw is not None and len(mmw.points) > 0 and mmw.micro_doppler_human_score < 0.45
        cold_non_living_thermal = thm is not None and thm.human_body_temp_ratio < 0.25 and thm.max_temp_c < 31.5

        cable_mist_filtered = no_uwb_danger and (non_human_clutter or cold_non_living_thermal)
        flags.append(
            AntiFalseAlarmFlag(
                rule_name="CABLE_SWAY_WATER_MIST_FILTER",
                triggered=cable_mist_filtered,
                description="透過 Thermal 排除無生命體溫散射體，結合 UWB 與微多普勒消除晃動電纜與水霧誤急停",
            )
        )

        if cable_mist_filtered:
            reasons.append("雷達偵測懸浮散射體，但 Thermal 無活體人體溫度且 UWB 無人員近距，判定為晃動電纜／水霧並消除誤急停")
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
                confidence=0.91,
            )

        # 真陽性碰撞風險評估（ISO/TS 15066 距離與速度分級）
        uwb_danger = csi is not None and (
            csi.uwb_nlos_alert or (csi.uwb_tag_distance_m is not None and csi.uwb_tag_distance_m <= 2.2)
        )
        radar_danger = (
            mmw is not None
            and mmw.target_distance_m <= 1.9
            and mmw.horizontal_speed_mps >= 0.45
            and mmw.micro_doppler_human_score >= 0.50
        )
        thermal_living = thm is None or thm.human_body_temp_ratio >= 0.30

        if (uwb_danger or radar_danger) and thermal_living:
            uwb_d = csi.uwb_tag_distance_m if (csi and csi.uwb_tag_distance_m is not None) else 9.9
            rad_d = mmw.target_distance_m if mmw else 9.9
            min_dist = min(uwb_d, rad_d)

            if uwb_danger and csi is not None:
                reasons.append(f"UWB 跨貨架 NLOS 盲區預警：作業員距離僅 {uwb_d:.2f}m")
            if radar_danger and mmw is not None:
                reasons.append(
                    f"mmWave 穿透粉塵偵測人員逼近 (距離 {rad_d:.2f}m, 速度 {mmw.horizontal_speed_mps:.2f}m/s)"
                )
            if rgb is not None and not rgb.ppe_compliant:
                reasons.append("L2 邊緣視覺偵測作業員未依規配戴安全帽/PPE")

            is_estop = min_dist <= 1.0 or (rgb is not None and not rgb.ppe_compliant)
            risk = RiskLevel.CRITICAL if is_estop else RiskLevel.HIGH
            rec_level = PrivacyLevel.L3 if is_estop else PrivacyLevel.L2
            action = "ISO15066_EMERGENCY_STOP" if is_estop else "ISO15066_SPEED_REDUCTION_50PCT"

            commands.append(
                {
                    "protocol": "PROFINET / Safety PLC Modbus TCP",
                    "target": "協作機械手臂與無人搬運車 AGV 安全控制器",
                    "action": action,
                    "payload": {
                        "min_distance_m": round(min_dist, 3),
                        "speed_limit_pct": 0 if is_estop else 50,
                        "uwb_nlos_alert": bool(csi.uwb_nlos_alert) if csi else False,
                    },
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
                confidence=round(max(ds_result.belief_alert, 0.84 if is_estop else 0.74), 4),
            )

        reasons.append("人機協作安全半徑內無碰撞風險，產線正常全速運作")
        return ScenarioEvaluation(
            scenario_id=self.scenario_id,
            scenario_name=self.name,
            risk_level=RiskLevel.SAFE,
            triggered=False,
            false_alarm_suppressed=False,
            trigger_reasons=reasons,
            anti_false_alarm_flags=flags,
            northbound_commands=[],
            recommended_privacy_level=PrivacyLevel.L1 if (mmw and mmw.breathing_confidence > 0.5) else PrivacyLevel.L0,
            confidence=round(max(ds_result.belief_normal, 0.86), 4),
        )
