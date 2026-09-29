"""Audio capture: microphones / virtual cables via sounddevice, Windows system audio via WASAPI loopback."""

from __future__ import annotations

import sys
import threading
import warnings
from typing import Callable

import numpy as np
import sounddevice as sd

from .devices import SYSTEM_LOOPBACK_ID, Device, com_init, com_uninit, is_wasapi, resolve_index
from .util import to_mono

# on_audio(mono_float32_block, sample_rate) is called from an audio thread.
AudioCallback = Callable[[np.ndarray, int], None]
ErrorCallback = Callable[[Exception], None]

BLOCK_MS = 20


class Capture:
    rate: int

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


class InputCapture(Capture):
    """Microphone or any input device (BlackHole, VB-CABLE output ...)."""

    def __init__(self, device: Device | None, on_audio: AudioCallback):
        index = resolve_index(device.name if device else None, "input")
        info = sd.query_devices(index, "input")
        self.rate = int(info["default_samplerate"])
        channels = max(1, min(2, int(info["max_input_channels"])))
        extra = sd.WasapiSettings(auto_convert=True) if is_wasapi(index) else None
        self._on_audio = on_audio
        self._stream = sd.InputStream(
            device=index,
            samplerate=self.rate,
            channels=channels,
            dtype="float32",
            blocksize=int(self.rate * BLOCK_MS / 1000),
            latency="low",
            extra_settings=extra,
            callback=self._callback,
        )

    def _callback(self, indata, frames, time_info, status) -> None:
        self._on_audio(to_mono(indata), self.rate)

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        try:
            self._stream.stop()
        finally:
            self._stream.close()


class LoopbackCapture(Capture):
    """Everything played on a Windows output device (WASAPI loopback).

    soundcard delivers silence in real time while nothing is playing, so the
    stream stays continuous and the server-side VAD can detect pauses.
    """

    def __init__(self, device: Device, on_audio: AudioCallback, on_error: ErrorCallback):
        if sys.platform != "win32":
            raise RuntimeError("Sistem sesi yakalama yalnızca Windows'ta doğrudan desteklenir. macOS'ta BlackHole kullanın.")
        self.rate = 48000
        self._speaker_id = device.id.removeprefix("lb:")
        self._on_audio = on_audio
        self._on_error = on_error
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="loopback-capture", daemon=True)

    def _run(self) -> None:
        com_init()
        try:
            import soundcard as sc

            warnings.filterwarnings("ignore", category=sc.SoundcardRuntimeWarning)
            mic = sc.get_microphone(id=self._speaker_id, include_loopback=True)
            frames = int(self.rate * BLOCK_MS / 1000)
            with mic.recorder(samplerate=self.rate, channels=2, blocksize=frames // 2) as rec:
                while not self._stop.is_set():
                    self._on_audio(to_mono(rec.record(numframes=frames)), self.rate)
        except Exception as exc:  # device unplugged, exclusive mode, ...
            if not self._stop.is_set():
                self._on_error(exc)
        finally:
            com_uninit()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def open_capture(device: Device | None, on_audio: AudioCallback, on_error: ErrorCallback) -> Capture:
    if device is not None and device.id == SYSTEM_LOOPBACK_ID:
        from .process_loopback import ProcessLoopbackCapture

        return ProcessLoopbackCapture(on_audio, on_error)
    if device is not None and device.kind == "loopback":
        return LoopbackCapture(device, on_audio, on_error)
    return InputCapture(device, on_audio)
