"""Audio device discovery for Windows and macOS.

Device ids are stable strings saved in the settings file:
    in:<name>     an input device opened with sounddevice (mic, BlackHole, VB-CABLE ...)
    lb:<id>       Windows WASAPI loopback of an output device (via soundcard)
    out:<name>    an output device opened with sounddevice
An empty id means "system default".
"""

from __future__ import annotations

import sys
import unicodedata
from dataclasses import dataclass

import sounddevice as sd

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

SYSTEM_LOOPBACK_ID = "lb:system"  # everything playing except LiveDub (Windows process loopback)

_VIRTUAL_HINTS = ("cable", "blackhole", "vb-audio", "voicemeeter", "soundflower", "loopback audio", "virtual")
_MULTI_HINTS = ("multi-output", "aggregate", "çoklu çıkış", "birleşik")
_LOOP_TAGS = ("cable", "blackhole", "voicemeeter", "soundflower")


@dataclass(frozen=True)
class Device:
    id: str
    name: str
    kind: str  # "input" | "loopback" | "output"

    @property
    def virtual(self) -> bool:
        low = self.name.lower()
        return any(h in low for h in _VIRTUAL_HINTS)

    @property
    def label(self) -> str:
        if self.id == SYSTEM_LOOPBACK_ID:
            return "Sistem sesi: tüm uygulamalar, dublaj hariç (önerilir)"
        if self.kind == "loopback":
            return f"Sistem sesi: {self.name}"
        if self.kind == "input" and self.virtual:
            return f"Sanal kablo: {self.name}"
        if self.kind == "input":
            return f"Mikrofon: {self.name}"
        return self.name


def process_loopback_supported() -> bool:
    """Capturing all audio except our own process needs Windows 10 build 20348+ / Windows 11."""
    return IS_WINDOWS and sys.getwindowsversion().build >= 20348


def com_init() -> None:
    """Join the COM multithreaded apartment on this thread (needed for WASAPI loopback calls).

    Qt makes the GUI thread single-threaded, so worker threads cannot rely on a process-wide MTA.
    """
    if IS_WINDOWS:
        import ctypes

        try:
            # soundcard calls CoInitializeEx at import and treats "already initialized" as an
            # error, so it must be imported before we initialize COM on this thread.
            import soundcard  # noqa: F401
        except Exception:
            pass
        ctypes.windll.ole32.CoInitializeEx(None, 0)


def com_uninit() -> None:
    if IS_WINDOWS:
        import ctypes

        ctypes.windll.ole32.CoUninitialize()


SYSTEM_SOURCE = Device(SYSTEM_LOOPBACK_ID, "Tüm sistem sesi (dublaj hariç)", "loopback")


def _norm(name: str) -> str:
    return unicodedata.normalize("NFC", name).strip().casefold()


def preferred_hostapi() -> int | None:
    """WASAPI on Windows (lowest latency, same names as the loopback list), Core Audio on macOS."""
    wanted = "Windows WASAPI" if IS_WINDOWS else "Core Audio" if IS_MAC else None
    for i, api in enumerate(sd.query_hostapis()):
        if wanted and api["name"] == wanted:
            return i
    return None


def is_wasapi(index: int | None) -> bool:
    if not IS_WINDOWS or index is None:
        return False
    info = sd.query_devices(index)
    return sd.query_hostapis(info["hostapi"])["name"] == "Windows WASAPI"


def _sd_devices(kind: str) -> list[tuple[int, dict]]:
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    api = preferred_hostapi()
    result = []
    for i, dev in enumerate(sd.query_devices()):
        if dev[key] <= 0:
            continue
        if api is not None and dev["hostapi"] != api:
            continue
        result.append((i, dev))
    return result


def list_inputs() -> list[Device]:
    return [Device(f"in:{d['name']}", d["name"], "input") for _, d in _sd_devices("input")]


def list_outputs() -> list[Device]:
    return [Device(f"out:{d['name']}", d["name"], "output") for _, d in _sd_devices("output")]


def list_loopbacks() -> list[Device]:
    """System-audio capture sources. Windows only; on macOS use BlackHole as an input."""
    if not IS_WINDOWS:
        return []
    devices = [SYSTEM_SOURCE] if process_loopback_supported() else []
    try:
        import soundcard as sc

        devices += [Device(f"lb:{m.id}", m.name, "loopback") for m in sc.all_microphones(include_loopback=True) if m.isloopback]
    except Exception:
        pass
    return devices


def list_sources() -> list[Device]:
    return list_loopbacks() + list_inputs()


def default_output() -> Device | None:
    api = preferred_hostapi()
    try:
        idx = sd.query_hostapis(api)["default_output_device"] if api is not None else sd.default.device[1]
        if idx is None or idx < 0:
            return None
        return Device("", sd.query_devices(idx)["name"], "output")
    except Exception:
        return None


_HEADPHONE_HINTS = ("headphone", "kulaklık", "airpods", "buds", "earphone", "ear ")


def physical_output() -> Device | None:
    """A real output to play the dub on: headphones if connected, otherwise the first speaker."""
    candidates = [d for d in list_outputs() if not d.virtual and not any(h in _norm(d.name) for h in _MULTI_HINTS)]
    for dev in candidates:
        if any(h in _norm(dev.name) for h in _HEADPHONE_HINTS):
            return dev
    return candidates[0] if candidates else None


def find_source(device_id: str) -> Device | None:
    if not device_id:
        return None
    for dev in list_sources():
        if dev.id == device_id:
            return dev
    return None


def find_output(device_id: str) -> Device | None:
    if not device_id:
        return default_output()
    for dev in list_outputs():
        if dev.id == device_id:
            return dev
    return None


def resolve_index(name: str | None, kind: str) -> int | None:
    """Map a device name back to a sounddevice index (indices change when devices are plugged)."""
    if not name:
        api = preferred_hostapi()
        if api is not None:
            key = "default_input_device" if kind == "input" else "default_output_device"
            idx = sd.query_hostapis(api)[key]
            if idx is not None and idx >= 0:
                return idx
        return None
    for idx, dev in _sd_devices(kind):
        if _norm(dev["name"]) == _norm(name):
            return idx
    raise RuntimeError(f"Ses cihazı bulunamadı: {name}")


def feedback_risk(source: Device, output: Device | None) -> bool:
    """True when the dubbed audio we play can leak back into the captured source."""
    if output is None:
        return source.kind == "input" and not source.virtual
    if source.id == SYSTEM_LOOPBACK_ID:
        return False  # our own output is excluded from this capture
    src, out = _norm(source.name), _norm(output.name)
    if source.kind == "loopback":
        return src == out
    if any(h in out for h in _MULTI_HINTS):
        return True
    if any(tag in src and tag in out for tag in _LOOP_TAGS):
        return True
    if source.virtual:
        return False
    # A physical microphone near physical speakers hears the dub.
    return not output.virtual
