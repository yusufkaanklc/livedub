"""Windows process-loopback capture: all system audio EXCEPT this process (our own dub).

Uses WASAPI's AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK with
PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE (Windows 10 build 20348+ / Windows 11),
called through raw ctypes COM vtables so no extra dependency is needed.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import POINTER, WINFUNCTYPE, byref, c_long, c_uint32, c_ulong, c_void_p, sizeof

import numpy as np

from .devices import com_init, com_uninit, process_loopback_supported as supported

VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE = 1
SHAREMODE_SHARED = 0
STREAMFLAGS_LOOPBACK = 0x00020000
STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
BUFFERFLAGS_SILENT = 0x2
WAVE_FORMAT_IEEE_FLOAT = 3
VT_BLOB = 65
S_OK = 0
E_NOINTERFACE = -2147467262


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text: str) -> "GUID":
        g = cls()
        ctypes.oledll.ole32.CLSIDFromString(text, byref(g))
        return g


IID_IUNKNOWN = "{00000000-0000-0000-C000-000000000046}"
IID_IAGILE_OBJECT = "{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}"
IID_COMPLETION_HANDLER = "{41D949AB-9862-444A-80F6-C261334DA5EB}"
IID_IAUDIO_CLIENT = "{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}"
IID_IAUDIO_CAPTURE_CLIENT = "{C8ADBD64-E71E-48A0-A4DE-185C395CD317}"


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("wFormatTag", ctypes.c_ushort), ("nChannels", ctypes.c_ushort), ("nSamplesPerSec", c_ulong),
        ("nAvgBytesPerSec", c_ulong), ("nBlockAlign", ctypes.c_ushort), ("wBitsPerSample", ctypes.c_ushort),
        ("cbSize", ctypes.c_ushort),
    ]


class LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", c_ulong), ("ProcessLoopbackMode", ctypes.c_int)]


class ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", LOOPBACK_PARAMS)]


class BLOB(ctypes.Structure):
    _fields_ = [("cbSize", c_ulong), ("pBlobData", c_void_p)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort), ("blob", BLOB)]


def _method(ptr: int, index: int, *argtypes):
    """Bind vtable slot ``index`` of COM object ``ptr``; failures raise OSError via HRESULT."""
    vtable = ctypes.cast(ptr, POINTER(c_void_p))[0]
    address = ctypes.cast(vtable, POINTER(c_void_p))[index]
    fn = WINFUNCTYPE(ctypes.HRESULT, c_void_p, *argtypes)(address)
    return lambda *args: fn(ptr, *args)


def _release(ptr: int | None) -> None:
    if ptr:
        vtable = ctypes.cast(ptr, POINTER(c_void_p))[0]
        WINFUNCTYPE(c_ulong, c_void_p)(ctypes.cast(vtable, POINTER(c_void_p))[2])(ptr)


_QI = WINFUNCTYPE(c_long, c_void_p, POINTER(GUID), POINTER(c_void_p))
_REF = WINFUNCTYPE(c_ulong, c_void_p)
_COMPLETED = WINFUNCTYPE(c_long, c_void_p, c_void_p)


class _CompletionHandler:
    """Minimal agile COM object implementing IActivateAudioInterfaceCompletionHandler."""

    def __init__(self):
        self.done = threading.Event()
        self.hr = -1
        self.client: int | None = None
        self._iids = {bytes(GUID.parse(i)) for i in (IID_IUNKNOWN, IID_IAGILE_OBJECT, IID_COMPLETION_HANDLER)}
        self._callbacks = (_QI(self._query), _REF(lambda this: 2), _REF(lambda this: 1), _COMPLETED(self._completed))
        self._vtable = (c_void_p * 4)(*[ctypes.cast(cb, c_void_p) for cb in self._callbacks])
        self._object = (c_void_p * 1)(ctypes.addressof(self._vtable))
        self.ptr = ctypes.addressof(self._object)

    def _query(self, this, riid, ppv) -> int:
        if bytes(riid.contents) in self._iids:
            ppv[0] = this
            return S_OK
        ppv[0] = None
        return E_NOINTERFACE

    def _completed(self, this, operation) -> int:
        try:
            hr, unknown = c_long(), c_void_p()
            _method(operation, 3, POINTER(c_long), POINTER(c_void_p))(byref(hr), byref(unknown))
            self.hr, self.client = hr.value, unknown.value
        except OSError as exc:
            self.hr = exc.winerror or -1
        finally:
            self.done.set()
        return S_OK


class ProcessLoopbackCapture:
    """Delivers mono float32 blocks at 48 kHz of everything playing except LiveDub itself."""

    rate = 48000
    BLOCK_S = 0.02

    def __init__(self, on_audio, on_error):
        if not supported():
            raise RuntimeError("'Tüm sistem sesi (dublaj hariç)' için Windows 10 (build 20348+) veya Windows 11 gerekir.")
        self._on_audio = on_audio
        self._on_error = on_error
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._start_error: Exception | None = None
        self._thread = threading.Thread(target=self._run, name="process-loopback", daemon=True)

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(10)
        if self._start_error is not None:
            raise self._start_error

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)

    def _activate(self) -> int:
        params = ACTIVATION_PARAMS(ACTIVATION_TYPE_PROCESS_LOOPBACK,
                                   LOOPBACK_PARAMS(os.getpid(), LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE))
        prop = PROPVARIANT(vt=VT_BLOB, blob=BLOB(sizeof(params), ctypes.cast(ctypes.pointer(params), c_void_p)))
        # Windows may call AddRef/Release on the handler later: keep it alive with the capture.
        self._handler = handler = _CompletionHandler()
        operation = c_void_p()
        activate = ctypes.WinDLL("Mmdevapi.dll").ActivateAudioInterfaceAsync
        activate.argtypes = [ctypes.c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT), c_void_p, POINTER(c_void_p)]
        activate.restype = ctypes.HRESULT
        activate(VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK, byref(GUID.parse(IID_IAUDIO_CLIENT)), byref(prop),
                 handler.ptr, byref(operation))
        try:
            if not handler.done.wait(5):
                raise RuntimeError("Windows ses yakalamayı başlatmadı (zaman aşımı).")
            if handler.hr < 0 or not handler.client:
                raise OSError(None, "Sistem sesi yakalama açılamadı", None, handler.hr)
            return handler.client
        finally:
            _release(operation.value)

    def _run(self) -> None:
        com_init()
        client = capture = None
        try:
            client = self._activate()
            # 32-bit float stereo: keeps full precision when other apps are turned down to ~1 %
            # and we boost the capture back up (see sessions.OriginalDucker).
            fmt = WAVEFORMATEX(WAVE_FORMAT_IEEE_FLOAT, 2, self.rate, self.rate * 8, 8, 32, 0)
            flags = STREAMFLAGS_LOOPBACK | STREAMFLAGS_AUTOCONVERTPCM | STREAMFLAGS_SRC_DEFAULT_QUALITY
            _method(client, 3, ctypes.c_int, c_ulong, ctypes.c_longlong, ctypes.c_longlong, POINTER(WAVEFORMATEX), c_void_p)(
                SHAREMODE_SHARED, flags, 2_000_000, 0, byref(fmt), None)  # 200 ms buffer
            service = c_void_p()
            _method(client, 14, POINTER(GUID), POINTER(c_void_p))(byref(GUID.parse(IID_IAUDIO_CAPTURE_CLIENT)), byref(service))
            capture = service.value
            _method(client, 10)()  # Start
        except Exception as exc:
            self._start_error = exc
            self._ready.set()
            _release(capture)
            _release(client)
            com_uninit()
            return
        self._ready.set()

        get_buffer = _method(capture, 3, POINTER(c_void_p), POINTER(c_uint32), POINTER(c_ulong), c_void_p, c_void_p)
        release_buffer = _method(capture, 4, c_uint32)
        next_packet = _method(capture, 5, POINTER(c_uint32))
        data, frames, flags, pending = c_void_p(), c_uint32(), c_ulong(), c_uint32()
        started = time.monotonic()
        emitted = 0
        try:
            while not self._stop.is_set():
                time.sleep(0.01)
                got = False
                next_packet(byref(pending))
                while pending.value:
                    get_buffer(byref(data), byref(frames), byref(flags), None, None)
                    count = frames.value
                    if flags.value & BUFFERFLAGS_SILENT or not data.value:
                        block = np.zeros(count, dtype=np.float32)
                    else:
                        raw = np.ctypeslib.as_array(ctypes.cast(data, POINTER(ctypes.c_float)), shape=(count * 2,))
                        block = raw.reshape(-1, 2).mean(axis=1, dtype=np.float32)
                    release_buffer(count)
                    if count:
                        self._on_audio(block, self.rate)
                        emitted += count
                        got = True
                    next_packet(byref(pending))
                # Windows sends nothing while all other apps are silent; keep the stream continuous
                # so server-side VAD still sees the pauses.
                behind = int((time.monotonic() - started) * self.rate) - emitted
                if not got and behind > self.rate * 0.1:
                    self._on_audio(np.zeros(behind, dtype=np.float32), self.rate)
                    emitted += behind
        except Exception as exc:
            if not self._stop.is_set():
                self._on_error(exc)
        finally:
            try:
                _method(client, 11)()  # Stop
            except OSError:
                pass
            _release(capture)
            _release(client)
            com_uninit()
