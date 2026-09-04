import asyncio
import logging
import time

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from xrt_source import list_devices, read_sensors

app = FastAPI()

POLL_INTERVAL_SEC = 1.0

logger = logging.getLogger("fpga.thermal")

# Last level logged per card. Polling runs once a second, so log only on a
# transition — otherwise a hot card floods the journal with identical lines.
# Concurrent WebSocket clients share this, which just dedupes them together.
_last_level: dict[str, str] = {}


def _log_alert(bdf: str, alert: dict) -> None:
    """Record threshold crossings so an overnight run can be audited after."""
    level = alert.get("level", "ok")
    if _last_level.get(bdf) == level:
        return
    was = _last_level.get(bdf)
    _last_level[bdf] = level

    if level == "ok":
        if was is not None:
            logger.info("[%s] 온도 정상 범위로 복귀", bdf)
        return

    detail = ", ".join(
        f"{s['description']} {s['celsius']:.0f}°C" for s in alert.get("sensors", [])
    )
    log = logger.error if level in ("critical", "fatal") else logger.warning
    log("[%s] %s — %s", bdf, alert.get("message", level), detail)


@app.get("/api/devices")
def get_devices():
    return {"devices": list_devices()}


@app.get("/api/sensors/{bdf}")
def get_sensors(bdf: str):
    return read_sensors(bdf)


@app.websocket("/ws/{bdf}")
async def sensor_stream(ws: WebSocket, bdf: str):
    await ws.accept()
    try:
        while True:
            try:
                data = await asyncio.to_thread(read_sensors, bdf)
                data["timestamp"] = time.time()
                _log_alert(bdf, data.get("alert", {}))
            except Exception as e:
                data = {"bdf": bdf, "error": str(e), "timestamp": time.time()}
            await ws.send_json(data)
            await asyncio.sleep(POLL_INTERVAL_SEC)
    except WebSocketDisconnect:
        pass


app.mount("/", StaticFiles(directory="../frontend", html=True), name="frontend")
