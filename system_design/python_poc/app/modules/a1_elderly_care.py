"""場景模組 A1：銀髮居家／長照衛浴「零穿戴、Privacy-by-Design」跌倒與生命徵象守護。"""

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


class A1ElderlyCareModule(ScenarioModule):
    """A1 長照衛浴／臥室跌倒偵測＋呼吸生命徵象＋寵物與水蒸氣防誤報模組。"""

    @property
    def scenario_id(self) -> ScenarioId:
        return ScenarioId.A1

    @property
    def name(self) -> str:
        return "A1 長照衛浴零穿戴跌倒與呼吸守護"

    @property
    def description(self) -> str:
        return "結合 Wi-Fi CSI 驟停、60GHz 高度落差與胸腔呼吸、VOx 人體溫度輪廓，精準過濾寵物與浴室水蒸氣。"

    @property
    def sensor_weights(self) -> dict[str, float]:
        return {"csi": 0.20, "mmwave": 0.40, "thermal": 0.25, "rgb": 0.15}

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

        # 防誤報規則 1：寵物（貓/狗/掃地機器人）低矮快速微動過濾
        pet_filtered = False
        if mmw is not None:
            is_low_nonhuman = (
                mmw.centroid_z_m < 0.50
                and mmw.micro_doppler_human_score < 0.45
                and mmw.vz_mps > -0.45
            )
            if is_low_nonhuman:
                pet_filtered = True
        flags.append(
            AntiFalseAlarmFlag(
                rule_name="PET_ROBOT_FILTER",
                triggered=pet_filtered,
                description="透過 60GHz 點雲質心高度與非人類微多普勒特徵排除寵物與掃地機器人",
            )
        )

        # 防誤報規則 2：浴室熱水蒸氣彌散溫升過濾（無集中人體輪廓且無垂直跌落軌跡）
        steam_filtered = False
        if thm is not None:
            diffuse_steam = thm.human_body_temp_ratio < 0.30 and thm.max_temp_c >= 38.0
            no_fall_kinematics = mmw is None or (mmw.vz_mps > -0.50 and mmw.centroid_z_m > 0.60)
            if diffuse_steam and no_fall_kinematics:
                steam_filtered = True
        flags.append(
            AntiFalseAlarmFlag(
                rule_name="BATHROOM_STEAM_FILTER",
                triggered=steam_filtered,
                description="熱像確認熱源呈彌散水蒸氣分佈而非集中人體體表輪廓（34–37°C），且雷達無跌落運動學",
            )
        )

        if pet_filtered or steam_filtered:
            if pet_filtered:
                reasons.append("偵測低質心與非人類微多普勒散射，判定為寵物／掃地機干擾並已抑制")
            if steam_filtered:
                reasons.append("衛浴環境為彌散熱蒸氣特徵，雷達無急墜軌跡，成功過濾水蒸氣誤報")
            return ScenarioEvaluation(
                scenario_id=self.scenario_id,
                scenario_name=self.name,
                risk_level=RiskLevel.SAFE,
                triggered=False,
                false_alarm_suppressed=True,
                trigger_reasons=reasons,
                anti_false_alarm_flags=flags,
                northbound_commands=[],
                recommended_privacy_level=PrivacyLevel.L0,
                confidence=round(1.0 - ds_result.belief_alert, 4),
            )

        # 真陽性跌倒與呼吸異常判定
        radar_fall = mmw is not None and (
            mmw.vz_mps <= -0.80
            or (mmw.centroid_z_m <= 0.55 and mmw.micro_doppler_human_score >= 0.50)
        )
        thermal_human = thm is None or (
            thm.human_body_temp_ratio >= 0.35 and 33.0 <= thm.max_temp_c <= 40.5
        )
        apnea_or_critical_breath = mmw is not None and (
            mmw.breathing_bpm < 8.0 or mmw.breathing_bpm > 28.0 or mmw.breathing_confidence < 0.35
        )
        rgb_fallen = rgb is not None and rgb.posture_label == "fallen"

        if radar_fall and thermal_human:
            reasons.append(
                f"60GHz 雷達偵測人體高度急墜／倒地靜止 (Z={mmw.centroid_z_m:.2f}m, Vz={mmw.vz_mps:.2f}m/s)"
            )
            if thm is not None:
                reasons.append(
                    f"Thermal 確認 34–37°C 人體熱特徵 (最高 {thm.max_temp_c:.1f}°C, 佔比 {thm.human_body_temp_ratio:.0%})"
                )
            if apnea_or_critical_breath and mmw is not None:
                reasons.append(
                    f"偵測呼吸微動異常／微弱 ({mmw.breathing_bpm:.1f} bpm, conf={mmw.breathing_confidence:.2f})"
                )
            if rgb_fallen:
                reasons.append("L2 Edge 17 點去識別化骨架確認水平倒地姿態")

            is_critical = apnea_or_critical_breath or rgb_fallen or ds_result.belief_alert >= 0.80
            risk = RiskLevel.CRITICAL if is_critical else RiskLevel.HIGH
            rec_level = PrivacyLevel.L3 if is_critical else PrivacyLevel.L2
            commands.append(
                {
                    "protocol": "MQTT / SIP",
                    "target": "護理站呼叫系統與家屬緊急通報端",
                    "action": "EMERGENCY_FALL_ALERT" if is_critical else "FALL_SUSPECTED_L2_VERIFY",
                    "payload": {
                        "centroid_z_m": mmw.centroid_z_m if mmw else 0.0,
                        "breathing_bpm": mmw.breathing_bpm if mmw else 0.0,
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
                confidence=round(max(ds_result.belief_alert, 0.78 if is_critical else 0.65), 4),
            )

        # 日常活動（安全移動或靜態正常呼吸）
        csi_motion = csi is not None and csi.fresnel_variance > 0.25
        radar_normal_person = mmw is not None and mmw.micro_doppler_human_score >= 0.50
        if csi_motion or radar_normal_person:
            reasons.append("偵測長者正常日常起居活動，呼吸與姿態平穩（保持無影像 L1 守護）")
            return ScenarioEvaluation(
                scenario_id=self.scenario_id,
                scenario_name=self.name,
                risk_level=RiskLevel.SAFE,
                triggered=False,
                false_alarm_suppressed=False,
                trigger_reasons=reasons,
                anti_false_alarm_flags=flags,
                northbound_commands=[],
                recommended_privacy_level=PrivacyLevel.L1,
                confidence=round(max(ds_result.belief_normal, 0.75), 4),
            )

        reasons.append("場域無人活動，維持 L0 極低功耗無線常駐感知")
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
