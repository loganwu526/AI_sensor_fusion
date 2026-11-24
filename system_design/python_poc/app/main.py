"""四模態 AI 感測融合與 Privacy-by-Design 即時監控 FastAPI 應用程式入口。

提供：
- 單檔零依賴即時監控戰情室首頁（`GET /`）
- 系統健康檢查、即時狀態快照與完整模擬目錄查詢 API
- 五大場景模組熱切換與三大硬體部署形態切換 API
- 11 種標準／真異常／抗誤報干擾情境即時注入與時間快進 API
- L3 雙因子緊急授權存證與揮發性 RAM 清零稽核日誌 API
- 8 Hz 即時遙測 WebSocket 推播通道（`/ws/telemetry`）
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .models import (
    HardwareProfileId,
    L3AuthorizeRequest,
    ScenarioId,
    SourceMode,
    SystemStateSnapshot,
)
from .simulator import INJECTABLE_EVENTS, SensorFusionSystem


class ReplayControlRequest(BaseModel):
    """回放播放／暫停／拖曳進度控制請求。"""

    playing: Optional[bool] = None
    seek_index: Optional[int] = None
    speed: Optional[float] = None
    loop: Optional[bool] = None
    step_once: bool = False


class CameraToggleRequest(BaseModel):
    """切換混合實況鏡頭來源（browser_getusermedia / local_opencv）。"""

    enabled: bool = True
    source: str = "browser_getusermedia"

BASE_DIR = Path(__file__).resolve().parent.parent
STATIC_INDEX = BASE_DIR / "static" / "index.html"
STATIC_DEMO = BASE_DIR / "static" / "demo.html"
START_TIME = time.monotonic()

app = FastAPI(
    title="四模態 AI 感測融合與 Privacy-by-Design 戰情原型",
    description="Wi-Fi 802.11bf CSI/BLE/UWB + 60/77GHz mmWave + VOx Thermal + Edge IP Camera 四模態融合 FastAPI POC",
    version="0.1.0",
)

# 系統全域實例（支援測試確定性步進與 WebSocket 8 Hz 即時推送）
fusion_system = SensorFusionSystem()


@app.get("/", include_in_schema=False)
async def serve_dashboard() -> FileResponse:
    """提供單檔零外部依賴即時監控戰情室網頁。"""
    if not STATIC_INDEX.exists():
        raise HTTPException(status_code=404, detail="找不到 static/index.html 戰情室檔案")
    return FileResponse(STATIC_INDEX, media_type="text/html; charset=utf-8")


@app.get("/api/health")
async def get_health() -> dict:
    """回傳系統健康狀態、目前啟用場景、硬體形態與隱私階級。"""
    snap = fusion_system.get_snapshot()
    return {
        "status": "ok",
        "service": "ai-sensor-fusion-poc",
        "version": "0.1.0",
        "uptime_s": round(time.monotonic() - START_TIME, 2),
        "sim_time_s": snap.sim_time_s,
        "active_scenario": snap.active_scenario.value,
        "active_hardware": snap.hardware_profile.id.value,
        "privacy_level": snap.privacy_level.value,
        "zero_disk_persistence": snap.zero_disk_persistence,
    }


@app.get("/api/state", response_model=SystemStateSnapshot)
async def get_state() -> SystemStateSnapshot:
    """取得當前完整融合狀態與遙測快照。"""
    return fusion_system.get_snapshot()


@app.get("/api/scenarios")
async def list_scenarios() -> dict:
    """列出五大可插拔場景模組、三大硬體藍本與 11 種可注入測試情境。"""
    return fusion_system.list_catalog()


@app.post("/api/scenario/{module_id}", response_model=SystemStateSnapshot)
async def switch_scenario(module_id: str) -> SystemStateSnapshot:
    """熱切換五大旗艦場景模組（A1 / C4 / B1 / C1 / B2）。"""
    try:
        sid = ScenarioId(module_id.upper())
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"不支援的場景模組 '{module_id}'，可用選項：A1, C4, B1, C1, B2",
        ) from exc
    return fusion_system.switch_scenario(sid)


@app.post("/api/hardware/{profile_id}", response_model=SystemStateSnapshot)
async def switch_hardware(profile_id: str) -> SystemStateSnapshot:
    """切換三大硬體部署形態（phase1_distributed_mvp / phase2_all_in_one_tier2 / tier3_industrial）。"""
    try:
        pid = HardwareProfileId(profile_id)
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"不支援的硬體形態 '{profile_id}'",
        ) from exc
    return fusion_system.switch_hardware(pid)


@app.post("/api/inject/{event_name}", response_model=SystemStateSnapshot)
async def inject_event(
    event_name: str,
    fast_forward_s: Optional[float] = Query(
        None, ge=0.0, le=3600.0, description="選填：注入時同時快進模擬時間（秒）"
    ),
) -> SystemStateSnapshot:
    """即時注入標準、真異常或故意誤報干擾測試情境。"""
    if event_name not in INJECTABLE_EVENTS:
        raise HTTPException(
            status_code=404,
            detail=f"未知的情境事件 '{event_name}'，可用事件請查詢 /api/scenarios",
        )
    return fusion_system.inject_event(event_name, fast_forward_s=fast_forward_s)


@app.post("/api/fast-forward/{seconds}", response_model=SystemStateSnapshot)
async def fast_forward_time(seconds: float) -> SystemStateSnapshot:
    """快進模擬時間以驗證 L3/L2/L1 遲滯防抖（Hysteresis）安全降階。"""
    if seconds < 0 or seconds > 3600:
        raise HTTPException(status_code=400, detail="快進時間須介於 0 至 3600 秒")
    return fusion_system.fast_forward(seconds)


@app.post("/api/stress/{enabled}", response_model=SystemStateSnapshot)
async def toggle_npu_stress(enabled: bool) -> SystemStateSnapshot:
    """切換高算力壓力負載模擬，以測試 Graceful Degradation 降幀保護。"""
    return fusion_system.set_burst_stress(enabled)


@app.post("/api/l3/authorize")
async def authorize_l3(
    payload: L3AuthorizeRequest = Body(default_factory=L3AuthorizeRequest),
) -> dict:
    """模擬雙因子授權（RBAC + OTP）啟封 L3 加密存證或解除警報。"""
    ok, msg, snap = fusion_system.authorize_l3(
        operator_id=payload.operator_id,
        otp_token=payload.otp_token,
        action=payload.action,
        reason=payload.reason,
    )
    if not ok:
        raise HTTPException(status_code=403, detail=msg)
    return {
        "authorized": snap.l3_authorized,
        "message": msg,
        "state": snap.model_dump(mode="json"),
    }


@app.get("/api/audit")
async def get_audit_logs(limit: int = Query(30, ge=1, le=100)) -> dict:
    """讀取最近 Volatile RAM 覆寫清零紀錄與系統稽核事件。"""
    snap = fusion_system.get_snapshot()
    return {
        "count": len(fusion_system.fsm.get_audits(limit)),
        "last_ram_wipe_ms": snap.last_ram_wipe_ms,
        "zero_disk_persistence": snap.zero_disk_persistence,
        "audits": [a.model_dump(mode="json") for a in fusion_system.fsm.get_audits(limit)],
    }


@app.post("/api/mode/{mode}", response_model=SystemStateSnapshot)
async def switch_source_mode(mode: str) -> SystemStateSnapshot:
    """切換系統感測輸入來源（simulator / log_replay / hybrid_live）。"""
    try:
        sm = SourceMode(mode.lower())
    except ValueError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"不支援的資料來源模式 '{mode}'，可用選項：simulator, log_replay, hybrid_live",
        ) from exc
    return fusion_system.set_source_mode(sm)


@app.get("/api/replay/tracks")
async def list_replay_tracks() -> dict:
    """列出所有內建與外部匯入之時間軸資料集摘要。"""
    return {
        "tracks": [t.model_dump() for t in fusion_system.replay_engine.list_tracks()],
        "status": fusion_system.replay_engine.get_status().model_dump(),
    }


@app.post("/api/replay/load/{track_id}", response_model=SystemStateSnapshot)
async def load_replay_track(track_id: str) -> SystemStateSnapshot:
    """載入指定時間軸資料集並自動切至對應應用場景與回放模式。"""
    try:
        return fusion_system.load_replay_track(track_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/replay/control", response_model=SystemStateSnapshot)
async def control_replay(
    payload: ReplayControlRequest = Body(default_factory=ReplayControlRequest),
) -> SystemStateSnapshot:
    """控制時間軸回放（播放／暫停／拖曳進度／倍速／單步）。"""
    return fusion_system.control_replay(
        playing=payload.playing,
        seek_index=payload.seek_index,
        speed=payload.speed,
        loop=payload.loop,
        step_once=payload.step_once,
    )


@app.post("/api/replay/evaluate")
async def evaluate_replay_tracks(
    track_id: Optional[str] = Query(None, description="指定評估軌跡 ID；省略則評估全部 5+ 軌跡"),
) -> dict:
    """執行公開／實測時間軸高層批次評估（Precision / Recall / F1 / 誤報削減率）。"""
    try:
        return fusion_system.evaluate_replay(track_id=track_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/replay/upload")
async def upload_replay_dataset(
    file: UploadFile = File(...),
    fmt: str = Form("auto"),
    scenario_id: str = Form("A1"),
) -> dict:
    """上傳外部公開感測檔案（JSONL / ESP-CSI CSV / TI mmWave CSV / Thermal CSV）並轉檔回放。"""
    try:
        sid = ScenarioId(scenario_id.upper())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"無效的場景 ID：{scenario_id}") from exc

    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="上傳檔案不可為空")

    try:
        summary, snap = fusion_system.upload_replay_dataset(
            filename=file.filename or "dataset.csv",
            content_bytes=raw_bytes,
            fmt=fmt,
            scenario_id=sid,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "track": summary.model_dump(),
        "state": snap.model_dump(mode="json"),
    }


@app.get("/api/camera/status")
async def get_camera_status() -> dict:
    """查詢混合實況攝影機隱私閘門與 Volatile RAM 清零狀態。"""
    snap = fusion_system.get_snapshot()
    return {
        "source_mode": snap.source_mode.value,
        "privacy_level": snap.privacy_level.value,
        "live_camera_status": fusion_system.live_camera.get_status().model_dump(),
    }


@app.post("/api/camera/toggle")
async def toggle_camera_source(
    payload: CameraToggleRequest = Body(default_factory=CameraToggleRequest),
) -> SystemStateSnapshot:
    """設定混合實況鏡頭來源並即時套用隱私閘門。"""
    fusion_system.live_camera.set_enabled(payload.enabled, source=payload.source)
    if payload.enabled and fusion_system.source_mode != SourceMode.HYBRID_LIVE:
        return fusion_system.set_source_mode(SourceMode.HYBRID_LIVE)
    fusion_system.live_camera.enforce_privacy_gate(fusion_system.fsm.current_level)
    return fusion_system.step(dt_s=0.05, advance_replay=False)


async def _extract_jpeg_bytes(request: Request) -> bytes:
    """統一解析 Multipart / Base64 JSON / Raw Binary 影像輸入荷載。"""
    ctype = (request.headers.get("content-type") or "").lower()
    jpeg_bytes: bytes = b""
    if "multipart/form-data" in ctype:
        form = await request.form()
        up = form.get("file")
        if up is not None and hasattr(up, "read"):
            jpeg_bytes = await up.read()
    elif "application/json" in ctype:
        body = await request.json()
        b64 = str(body.get("image_base64", ""))
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        if b64:
            jpeg_bytes = base64.b64decode(b64)
    else:
        jpeg_bytes = await request.body()

    if not jpeg_bytes:
        raise HTTPException(status_code=400, detail="未接收到有效影像資料")
    return jpeg_bytes


@app.post("/api/camera/frame")
async def ingest_camera_frame(request: Request) -> dict:
    """接收來自瀏覽器 `getUserMedia` 的即時 JPEG 畫面。

    若當前系統處於 L0 / L1（Camera-Off），將硬性拒絕並不保留任何像素位元組；
    僅於 L2 / L3 在 Volatile RAM 執行去識別化 17 點骨架推論並即刻覆寫清零。
    """
    jpeg_bytes = await _extract_jpeg_bytes(request)
    return fusion_system.ingest_live_jpeg(jpeg_bytes, source_name="browser_getusermedia")


@app.get("/demo", include_in_schema=False)
async def serve_executive_demo() -> FileResponse:
    """提供高層 3 分鐘互動簡報頁面（真實 YOLOv8n-pose + 遮鏡失效驗證 + 官方 ESP32 資料）。"""
    if not STATIC_DEMO.exists():
        raise HTTPException(status_code=404, detail="找不到 static/demo.html 簡報頁面")
    return FileResponse(STATIC_DEMO, media_type="text/html; charset=utf-8")


@app.post("/api/demo/pose")
async def demo_live_pose(request: Request) -> dict:
    """高層互動簡報專用端點：執行真實 YOLOv8n-pose 骨架、遮鏡失明偵測與 RAM 清零實測。"""
    jpeg_bytes = await _extract_jpeg_bytes(request)
    return fusion_system.live_camera.process_demo_frame(jpeg_bytes)


@app.get("/api/demo/real-csi")
async def demo_real_public_csi() -> dict:
    """回傳下載自 GitHub 之真實 ESP32 硬體實測 CSV 出處、原始封包預覽與波形序列。"""
    return fusion_system.replay_engine.get_real_public_csi_payload(max_frames=180)


@app.websocket("/ws/telemetry")
async def telemetry_websocket(websocket: WebSocket) -> None:
    """WebSocket 即時遙測推送通道（約 8 Hz 推播節奏）。"""
    await websocket.accept()
    paused = False

    async def _send_loop() -> None:
        # 連線即刻推送當前狀態快照，確保首幀零等待
        initial = fusion_system.get_snapshot()
        await websocket.send_json(initial.model_dump(mode="json"))
        while True:
            await asyncio.sleep(0.125)  # 8 Hz 節奏
            if paused:
                snap = fusion_system.get_snapshot()
            else:
                snap = fusion_system.step(dt_s=0.125)
            await websocket.send_json(snap.model_dump(mode="json"))

    sender_task = asyncio.create_task(_send_loop())
    try:
        while True:
            raw = await websocket.receive_text()
            if "pause" in raw:
                paused = True
            elif "resume" in raw:
                paused = False
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass
