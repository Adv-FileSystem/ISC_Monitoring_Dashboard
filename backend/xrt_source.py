"""Reads live sensor data from a Xilinx/AMD Alveo card via xrt-smi."""
import json
import re
import subprocess
import tempfile
import os
import threading
import uuid

XRT_SMI = "/opt/xilinx/xrt/bin/xrt-smi"

# Thermal limits from the Alveo U50 data sheet (DS965), "Board and Temperature
# Limits". The card's satellite controller enforces these on its own — it cuts
# power to the FPGA at the fatal limit whether or not anything is watching.
# Mirroring them here only lets the dashboard say something before that happens.
FPGA_LIMITS = {"warning": 88.0, "critical": 97.0, "fatal": 107.0}
BOARD_LIMITS = {"warning": 100.0, "critical": 110.0, "fatal": 125.0}

# The card starts throttling around its warning limit, which quietly invalidates
# any benchmark still running. Flag a band below it so the run can be stopped
# while its numbers still mean something.
CAUTION_MARGIN_C = 8.0

LEVELS = ("ok", "caution", "warning", "critical", "fatal")

LEVEL_MESSAGES = {
    "caution": "온도 상승 — 스로틀링 임박, 벤치마크 결과가 오염될 수 있습니다",
    "warning": "AMD 경고 한계(88°C) 초과 — 스로틀링 구간입니다",
    "critical": "AMD 위험 한계 초과 — 부하를 즉시 중단하세요",
    "fatal": "AMD 치명 한계 초과 — SC 펌웨어가 FPGA 전원을 차단합니다",
}


def _limits_for(location: str) -> dict:
    """PCB sensors follow the board limits; everything else is FPGA package."""
    return BOARD_LIMITS if location.lower().startswith("pcb") else FPGA_LIMITS


def _classify(celsius: float, limits: dict) -> str:
    if celsius >= limits["fatal"]:
        return "fatal"
    if celsius >= limits["critical"]:
        return "critical"
    if celsius >= limits["warning"]:
        return "warning"
    if celsius >= limits["warning"] - CAUTION_MARGIN_C:
        return "caution"
    return "ok"


def _build_alert(temperatures: list[dict]) -> dict:
    """Worst level across sensors, plus the ones that are actually elevated."""
    worst = "ok"
    for t in temperatures:
        if LEVELS.index(t["level"]) > LEVELS.index(worst):
            worst = t["level"]
    return {
        "level": worst,
        "message": LEVEL_MESSAGES.get(worst, ""),
        "sensors": [
            {"description": t["description"], "celsius": t["celsius"], "level": t["level"]}
            for t in temperatures
            if t["level"] != "ok"
        ],
    }


def _pretty_name(shell: str) -> str:
    """xilinx_u50_gen3x16_xdma_base_5 -> 'Alveo U50'."""
    m = re.search(r"_(u\d+[a-z]*)_", shell.lower())
    return f"Alveo {m.group(1).upper()}" if m else shell


def list_devices() -> list[dict]:
    """Return devices xrt-smi reports as present, with a display name."""
    out = subprocess.run(
        [XRT_SMI, "examine"], capture_output=True, text=True, timeout=10
    ).stdout
    devices = []
    for line in out.splitlines():
        if line.startswith("|[") or line.startswith("| ["):
            cols = [c.strip() for c in line.split("|") if c.strip()]
            bdf, shell = cols[0].strip("[]"), cols[1]
            devices.append({"bdf": bdf, "name": _pretty_name(shell), "shell": shell})
    return devices


def read_sensors(bdf: str) -> dict:
    """Run xrt-smi examine for electrical+thermal and return parsed JSON."""
    # Unique per call: concurrent WebSocket connections each poll this function
    # in their own thread, and xrt-smi refuses to write to a path that already
    # exists, so a shared/fixed path races across concurrent calls.
    unique = f"{os.getpid()}_{threading.get_ident()}_{uuid.uuid4().hex}"
    path = os.path.join(tempfile.gettempdir(), f"xrt_report_{unique}.json")
    try:
        subprocess.run(
            [
                XRT_SMI, "examine",
                "-d", bdf,
                "-r", "electrical",
                "-r", "thermal",
                "-f", "JSON",
                "-o", path,
            ],
            capture_output=True, text=True, timeout=10, check=True,
        )
        with open(path) as f:
            data = json.load(f)
    finally:
        if os.path.exists(path):
            os.remove(path)

    device = data["devices"][0]
    electrical = device.get("electrical", {})
    thermals = device.get("thermals", [])

    power_rails = [
        {
            "id": r["id"],
            "description": r["description"],
            "volts": float(r["voltage"]["volts"]),
            "amps": float(r["current"]["amps"]),
        }
        for r in electrical.get("power_rails", [])
        if r["voltage"]["is_present"] == "true"
    ]

    temperatures = []
    for t in thermals:
        if t.get("is_present", "true") != "true":
            continue
        location = t["location_id"]
        celsius = float(t["temp_C"])
        limits = _limits_for(location)
        temperatures.append({
            "location": location,
            "description": t["description"],
            "celsius": celsius,
            "level": _classify(celsius, limits),
            "limits": limits,
        })

    return {
        "bdf": bdf,
        "status": device.get("device_status", "UNKNOWN"),
        "power_watts": float(electrical.get("power_consumption_watts", 0) or 0),
        "power_max_watts": float(electrical.get("power_consumption_max_watts", 0) or 0),
        "power_warning": electrical.get("power_consumption_warning", "false") == "true",
        "power_rails": power_rails,
        "temperatures": temperatures,
        "alert": _build_alert(temperatures),
    }
