"""場景模組 B1：關鍵園區／倉儲全天候「零誤報」周界入侵偵測與分級喚醒安防。"""

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


class B1PerimeterModule(ScenarioModule):
    """B1 周界入侵偵測＋BLE/UWB 保全白名單＋陽光反光與動物干擾過濾模組。"""

    @property
    def scenario_id(self) -> ScenarioId:
        return ScenarioId.B1

    @property
    def name(self) -> str:
        return "B1 關鍵園區全天候零誤報周界入侵"

    @property
    def description(self) -> str:
        return "整合 BLE/UWB 巡邏白名單核銷、mmWave 步態速度向量與 Thermal 恆溫體熱驗證，消除樹影、動物與陽光反光誤報。"

    @property
    def sensor_weights(self) -> dict[str, float]:
        return {"csi": 0.25, "mmwave": 0.35, "thermal": 0.25, "rgb": 0.15}

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

        # 防誤報規則 1：授權夜間巡邏保全 BLE/UWB 白名單自動核銷
        whitelist_ok = csi is not None and (csi.ble_whitelist_present or csi.uwb_authorized)
        flags.append(
            AntiFalseAlarmFlag(
                rule_name="AUTHORIZED_PATROL_WHITELIST",
                triggered=whitelist_ok,
                description="巡邏人員配戴加密 BLE/UWB 識別信標，自動核銷免觸發警報",
            )
        )

        # 防誤報規則 2：陽光眩光 / 柏油熱浪閃爍過濾
        sunlight_glare = False
        if thm is not None and (
            thm.is_glass_boundary_reflection
            or (thm.max_temp_c > 45.0 and (mmw is None or mmw.micro_doppler_human_score < 0.45))
        ):
            sunlight_glare = True
        if rgb is not None and rgb.vehicle_or_glare_heat_mask:
            sunlight_glare = True
        flags.append(
            AntiFalseAlarmFlag(
                rule_name="SUNLIGHT_GLARE_SHIMMER_FILTER",
                triggered=sunlight_glare,
                description="排除夏季柏油熱浪反光與強光炫影（無步態向量與人體熱斑）",
            )
        )

        # 防誤報規則 3：野狗／鹿／樹枝晃動過濾
        animal_or_foliage = False
        if mmw is not None and (mmw.centroid_z_m < 0.70 or mmw.micro_doppler_human_score < 0.45):
            if mmw.horizontal_speed_mps > 0.05 or len(mmw.points) > 0:
                animal_or_foliage = True
        flags.append(
            AntiFalseAlarmFlag(
                rule_name="WILDLIFE_FOLIAGE_FILTER",
                triggered=animal_or_foliage,
                description="依據目標質心高度 (<0.7m) 與非雙足步態微多普勒過濾動物與搖曳樹枝",
            )
        )

        if whitelist_ok or sunlight_glare or animal_or_foliage:
            if whitelist_ok:
                reasons.append("BLE/UWB 白名單核銷通過：確認為授權巡邏保全")
            if sunlight_glare:
                reasons.append("已識別為太陽反光／熱浪閃爍干擾並抑制")
            if animal_or_foliage:
                reasons.append("已識別為野生動物或風吹植栽干擾並過濾")
            return ScenarioEvaluation(
                scenario_id=self.scenario_id,
                scenario_name=self.name,
                risk_level=RiskLevel.SAFE,
                triggered=False,
                false_alarm_suppressed=True,
                trigger_reasons=reasons,
                anti_false_alarm_flags=flags,
                northbound_commands=[],
                recommended_privacy_level=PrivacyLevel.L1 if whitelist_ok else PrivacyLevel.L0,
                confidence=0.90,
            )

        # 真陽性周界入侵判定
        radar_human_intruder = (
            mmw is not None
            and mmw.micro_doppler_human_score >= 0.60
            and mmw.horizontal_speed_mps >= 0.40
            and mmw.centroid_z_m >= 0.80
        )
        thermal_human = thm is not None and (
            thm.human_body_temp_ratio >= 0.35 and 32.5 <= thm.max_temp_c <= 40.5
        )
        no_whitelist = csi is None or (not csi.ble_whitelist_present and not csi.uwb_authorized)

        if no_whitelist and radar_human_intruder and thermal_human:
            reasons.append("無有效 BLE/UWB 授權白名單，確認非授權人員逼近管制圍籬")
            reasons.append(
                f"mmWave 確認雙足步態逼近 (v={mmw.horizontal_speed_mps:.2f}m/s, gait={mmw.micro_doppler_human_score:.2f})"
            )
            reasons.append(
                f"Thermal 確認恆溫人體輪廓 ({thm.max_temp_c:.1f}°C)"
            )
            is_critical = (rgb is not None and rgb.person_detected) or ds_result.belief_alert >= 0.82
            risk = RiskLevel.CRITICAL if is_critical else RiskLevel.HIGH
            rec_level = PrivacyLevel.L3 if is_critical else PrivacyLevel.L2
            commands.append(
                {
                    "protocol": "ONVIF / VMS gRPC",
                    "target": "SOC 保全戰情中心與周界變焦 PTZ 攝影機",
                    "action": "PERIMETER_INTRUSION_LOCK_ON",
                    "payload": {
                        "target_distance_m": mmw.target_distance_m,
                        "speed_mps": mmw.horizontal_speed_mps,
                        "ds_belief_alert": ds_result.belief_alert,
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
                confidence=round(max(ds_result.belief_alert, 0.80), 4),
            )

        reasons.append("周界電子圍籬安全無侵入事件")
        return ScenarioEvaluation(
            scenario_id=self.scenario_id,
            scenario_name=self.name,
            risk_level=RiskLevel.SAFE,
            triggered=False,
            false_alarm_suppressed=False,
            trigger_reasons=reasons,
            anti_false_alarm_flags=flags,
            northbound_commands=[],
            recommended_privacy_level=PrivacyLevel.L0,
            confidence=round(max(ds_result.belief_normal, 0.85), 4),
        )
