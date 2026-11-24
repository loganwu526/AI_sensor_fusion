"""場景模組 B2：智慧商辦／醫療院所「微氣候 HVAC 零 False-Off 節能與非營業夜間安防」。"""

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


class B2SmartHvacModule(ScenarioModule):
    """B2 AP-as-a-Sensor 呼吸存在防誤關冷氣＋玻璃外行人與靜態熱源排除模組。"""

    @property
    def scenario_id(self) -> ScenarioId:
        return ScenarioId.B2

    @property
    def name(self) -> str:
        return "B2 智慧商辦微氣候 HVAC 零 False-Off 與夜間安防"

    @property
    def description(self) -> str:
        return "日間嚴格不開鏡 (L0/L1)，以 mmWave 靜坐呼吸確認防止 PIR 誤關冷氣，並以 32×24 Thermal 排除玻璃外走廊行人與靜態發熱電器。"

    @property
    def sensor_weights(self) -> dict[str, float]:
        return {"csi": 0.30, "mmwave": 0.35, "thermal": 0.25, "rgb": 0.10}

    def evaluate(
        self,
        aligned_frame: AlignedFrame,
        ds_result: DsFusionResult,
    ) -> ScenarioEvaluation:
        csi = aligned_frame.csi
        mmw = aligned_frame.mmwave
        thm = aligned_frame.thermal

        flags: list[AntiFalseAlarmFlag] = []
        reasons: list[str] = []
        commands: list[dict] = []

        # 防誤報規則 1：雷達穿透玻璃隔間偵測到走廊外路人（熱像界定玻璃內無對應人體）
        glass_passerby_filtered = thm is not None and thm.is_glass_boundary_reflection
        flags.append(
            AntiFalseAlarmFlag(
                rule_name="GLASS_PARTITION_PASSERBY_FILTER",
                triggered=glass_passerby_filtered,
                description="Thermal 界定熱輪廓不在玻璃隔間內，排除 mmWave 穿牆偵測走廊路人造成的徹夜誤開空調",
            )
        )

        # 防誤報規則 2：靜態發熱電器／咖啡機／伺服器主機排除（無胸腔呼吸微動）
        static_heat_filtered = (
            thm is not None
            and thm.is_static_heat_source
            and (mmw is None or mmw.breathing_confidence < 0.40)
        )
        flags.append(
            AntiFalseAlarmFlag(
                rule_name="STATIC_HEAT_SOURCE_FILTER",
                triggered=static_heat_filtered,
                description="透過 mmWave 胸腔微動缺失排除事務機、咖啡機等靜態熱源",
            )
        )

        if glass_passerby_filtered or static_heat_filtered:
            if glass_passerby_filtered:
                reasons.append("已識別穿牆雷達回波為玻璃隔間外走廊行人，判定辦公室內實為無人狀態")
            if static_heat_filtered:
                reasons.append("無呼吸微動特徵，已排除靜態發熱設備誤判在席")
            commands.append(
                {
                    "protocol": "BACnet/IP",
                    "target": "樓宇自動化 VAV 變風量箱與照明迴路",
                    "action": "HVAC_STANDBY_ENERGY_SAVING",
                    "payload": {"zone_occupied": False, "delay_cutoff_min": 5},
                }
            )
            return ScenarioEvaluation(
                scenario_id=self.scenario_id,
                scenario_name=self.name,
                risk_level=RiskLevel.SAFE,
                triggered=False,
                false_alarm_suppressed=True,
                trigger_reasons=reasons,
                anti_false_alarm_flags=flags,
                northbound_commands=commands,
                recommended_privacy_level=PrivacyLevel.L0,
                confidence=0.92,
            )

        # 日間靜坐人員呼吸存在確認（零 False-Off，嚴格保持 L1 無攝影機）
        stationary_breathing_occupied = (
            mmw is not None
            and mmw.breathing_confidence >= 0.60
            and 8.0 <= mmw.breathing_bpm <= 26.0
        )
        indoor_human_thermal = (
            thm is not None
            and thm.human_body_temp_ratio >= 0.30
            and not thm.is_glass_boundary_reflection
            and not thm.is_static_heat_source
        )

        if stationary_breathing_occupied and indoor_human_thermal:
            reasons.append(
                f"mmWave 偵測靜坐辦公人員穩定呼吸 ({mmw.breathing_bpm:.1f} bpm, conf={mmw.breathing_confidence:.2f})，徹底消除 PIR False-Off"
            )
            reasons.append(
                f"Thermal 確認玻璃內真實人體輻射熱負荷 ({thm.max_temp_c:.1f}°C)，日間強制鎖定 Camera-Off (L1)"
            )
            commands.append(
                {
                    "protocol": "BACnet/IP",
                    "target": "樓宇自動化 VAV 風閥與新風換氣系統",
                    "action": "HVAC_MAINTAIN_COMFORT_ACTIVE",
                    "payload": {
                        "zone_occupied": True,
                        "ambient_temp_c": thm.ambient_temp_c,
                        "radiant_peak_c": thm.max_temp_c,
                    },
                }
            )
            return ScenarioEvaluation(
                scenario_id=self.scenario_id,
                scenario_name=self.name,
                risk_level=RiskLevel.SAFE,
                triggered=True,
                false_alarm_suppressed=False,
                trigger_reasons=reasons,
                anti_false_alarm_flags=flags,
                northbound_commands=commands,
                recommended_privacy_level=PrivacyLevel.L1,
                confidence=round(max(ds_result.belief_normal, 0.88), 4),
            )

        # 若非營業時段偵測到高速非白名單入侵（高 D-S Alert）
        if (
            ds_result.belief_alert >= 0.65
            and csi is not None
            and not csi.ble_whitelist_present
            and mmw is not None
            and mmw.horizontal_speed_mps >= 0.5
        ):
            reasons.append("非營業時段偵測異常移動與人體熱斑，觸發夜間 L2 匿名安防確認")
            commands.append(
                {
                    "protocol": "MQTT / ONVIF",
                    "target": "大樓夜間保全中控台",
                    "action": "NIGHT_OFFICE_INTRUSION_VERIFY",
                    "payload": {"ds_belief_alert": ds_result.belief_alert},
                }
            )
            return ScenarioEvaluation(
                scenario_id=self.scenario_id,
                scenario_name=self.name,
                risk_level=RiskLevel.HIGH,
                triggered=True,
                false_alarm_suppressed=False,
                trigger_reasons=reasons,
                anti_false_alarm_flags=flags,
                northbound_commands=commands,
                recommended_privacy_level=PrivacyLevel.L2,
                confidence=round(ds_result.belief_alert, 4),
            )

        reasons.append("空間確認無人且無熱負荷，進入節能待機狀態")
        commands.append(
            {
                "protocol": "BACnet/IP",
                "target": "樓宇自動化 VAV 風閥",
                "action": "HVAC_ECO_IDLE",
                "payload": {"zone_occupied": False},
            }
        )
        return ScenarioEvaluation(
            scenario_id=self.scenario_id,
            scenario_name=self.name,
            risk_level=RiskLevel.SAFE,
            triggered=False,
            false_alarm_suppressed=False,
            trigger_reasons=reasons,
            anti_false_alarm_flags=flags,
            northbound_commands=commands,
            recommended_privacy_level=PrivacyLevel.L0,
            confidence=round(max(ds_result.belief_normal, 0.85), 4),
        )
