"""Windows per-app volume control (the Volume Mixer), used to silence the original audio.

Walks every audio session on every active playback device through raw ctypes COM calls and
lowers or mutes the ones that belong to other processes, remembering their levels so they can
be restored. Must run on a thread that called com_init().
"""

from __future__ import annotations

import ctypes
import json
import os
from ctypes import POINTER, byref, c_float, c_long, c_uint32, c_ulong, c_void_p

from ..config import CONFIG_DIR
from .process_loopback import GUID, _method, _release

MIN_LEVEL = 0.01  # quietest we can go: at 0 % Windows would hand us silence to translate
RECOVERY_FILE = CONFIG_DIR / "ducked_apps.json"
SYSTEM_SOUNDS = "<system sounds>"
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

CLSID_MM_DEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
IID_IMM_DEVICE_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
IID_IAUDIO_SESSION_MANAGER2 = "{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}"
IID_IAUDIO_SESSION_CONTROL2 = "{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}"
IID_ISIMPLE_AUDIO_VOLUME = "{87CE5498-68D6-44E5-9215-6DA47EF883D8}"
CLSCTX_ALL = 23
E_RENDER = 0
DEVICE_STATE_ACTIVE = 1


def _query(ptr: int, iid: str) -> int:
    out = c_void_p()
    _method(ptr, 0, POINTER(GUID), POINTER(c_void_p))(byref(GUID.parse(iid)), byref(out))
    return out.value


def _sessions():
    """Yield (process id, ISimpleAudioVolume*) for every session on active playback devices.

    The caller owns the yielded volume pointer and must _release it.
    """
    enumerator = c_void_p()
    ctypes.oledll.ole32.CoCreateInstance(byref(GUID.parse(CLSID_MM_DEVICE_ENUMERATOR)), None, CLSCTX_ALL,
                                         byref(GUID.parse(IID_IMM_DEVICE_ENUMERATOR)), byref(enumerator))
    collection = c_void_p()
    try:
        _method(enumerator.value, 3, ctypes.c_int, c_ulong, POINTER(c_void_p))(E_RENDER, DEVICE_STATE_ACTIVE, byref(collection))
        count = c_uint32()
        _method(collection.value, 3, POINTER(c_uint32))(byref(count))
        for d in range(count.value):
            device, manager, sessions = c_void_p(), c_void_p(), c_void_p()
            try:
                _method(collection.value, 4, c_uint32, POINTER(c_void_p))(d, byref(device))
                _method(device.value, 3, POINTER(GUID), c_ulong, c_void_p, POINTER(c_void_p))(
                    byref(GUID.parse(IID_IAUDIO_SESSION_MANAGER2)), CLSCTX_ALL, None, byref(manager))
                _method(manager.value, 5, POINTER(c_void_p))(byref(sessions))
                n = ctypes.c_int()
                _method(sessions.value, 3, POINTER(ctypes.c_int))(byref(n))
                for i in range(n.value):
                    control = c_void_p()
                    control2 = volume = None
                    try:
                        _method(sessions.value, 4, ctypes.c_int, POINTER(c_void_p))(i, byref(control))
                        control2 = _query(control.value, IID_IAUDIO_SESSION_CONTROL2)
                        pid = c_ulong()
                        try:
                            _method(control2, 14, POINTER(c_ulong))(byref(pid))
                        except OSError:
                            continue  # multi-process session without a single owner
                        volume = _query(control.value, IID_ISIMPLE_AUDIO_VOLUME)
                        yield pid.value, volume
                        volume = None  # ownership passed to the caller
                    finally:
                        _release(volume)
                        _release(control2)
                        _release(control.value)
            except OSError:
                continue
            finally:
                _release(sessions.value)
                _release(manager.value)
                _release(device.value)
    finally:
        _release(collection.value)
        _release(enumerator.value)


def _get_volume(volume: int) -> float:
    level = c_float()
    _method(volume, 4, POINTER(c_float))(byref(level))
    return level.value


def _set_volume(volume: int, level: float) -> None:
    _method(volume, 3, c_float, c_void_p)(min(1.0, max(0.0, level)), None)


def _exe_path(pid: int) -> str:
    if pid == 0:
        return SYSTEM_SOUNDS
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = c_ulong(len(buf))
        return buf.value if kernel32.QueryFullProcessImageNameW(handle, 0, buf, byref(size)) else ""
    finally:
        kernel32.CloseHandle(handle)


def set_own_volume(level: float, mute: bool) -> None:
    """Set this process's own session volume (used by tests)."""
    for pid, volume in _sessions():
        if pid == os.getpid():
            _set_volume(volume, level)
            _method(volume, 5, c_long, c_void_p)(int(mute), None)
        _release(volume)


class OriginalDucker:
    """Turns every other app down to ``level`` of its volume while dubbing, and back up afterwards.

    Windows' process loopback captures audio *after* the per-app volume, so apps cannot simply
    be muted: they are lowered (to 1 % at most) and the capture is boosted by ``gain``.
    Original levels are also written to disk so a crash cannot leave apps quiet: recover()
    puts them back on the next start. Call update() periodically from a COM-initialized thread.
    """

    def __init__(self, level: float):
        self.level = level
        self.gain = 1.0
        self._applied: float | None = None
        self._ducked: dict[tuple[int, int], tuple[int, float, str]] = {}  # (device, pid) -> (volume*, original, exe)

    def update(self) -> None:
        level = min(1.0, max(MIN_LEVEL, self.level))
        own = os.getpid()
        changed = False
        for key, pid, volume in _sessions_with_device():
            if pid == own or key in self._ducked or level >= 1.0:
                _release(volume)
                continue
            try:
                original = _get_volume(volume)
                _set_volume(volume, original * level)
            except OSError:
                _release(volume)
                continue
            self._ducked[key] = (volume, original, _exe_path(pid))
            changed = True
        if level != self._applied:
            for volume, original, _ in self._ducked.values():
                try:
                    _set_volume(volume, original * level)
                except OSError:
                    pass
            self._applied = level
        self.gain = 1.0 / level
        if changed:
            _save_recovery({exe: original for _, original, exe in self._ducked.values() if exe})

    def restore(self) -> None:
        restored = set()
        for volume, original, exe in self._ducked.values():
            try:
                _set_volume(volume, original)
                restored.add(exe)
            except OSError:
                pass  # the app has exited
            _release(volume)
        self._ducked.clear()
        self.gain = 1.0
        _forget_recovery(restored)


def recover() -> None:
    """Restore app volumes left lowered by a LiveDub run that did not shut down cleanly."""
    pending = _load_recovery()
    if not pending:
        return
    restored = set()
    for pid, volume in _sessions():
        exe = _exe_path(pid)
        if exe in pending:
            try:
                _set_volume(volume, pending[exe])
                restored.add(exe)
            except OSError:
                pass
        _release(volume)
    _forget_recovery(restored)


def _sessions_with_device():
    """Like _sessions() but keyed per device, since one process can play on several devices."""
    seen: dict[int, int] = {}
    for pid, volume in _sessions():
        index = seen.get(pid, 0)
        seen[pid] = index + 1
        yield (index, pid), pid, volume


def _load_recovery() -> dict[str, float]:
    try:
        return json.loads(RECOVERY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save_recovery(levels: dict[str, float]) -> None:
    merged = {**levels, **_load_recovery()}  # keep the oldest original if a crash left one behind
    try:
        RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
        RECOVERY_FILE.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def _forget_recovery(exes: set[str]) -> None:
    remaining = {exe: level for exe, level in _load_recovery().items() if exe not in exes}
    try:
        if remaining:
            RECOVERY_FILE.write_text(json.dumps(remaining, ensure_ascii=False), encoding="utf-8")
        else:
            RECOVERY_FILE.unlink(missing_ok=True)
    except OSError:
        pass
