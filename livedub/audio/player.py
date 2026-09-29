"""Low-latency output: plays the dub and (optionally) a ducked copy of the original audio."""

from __future__ import annotations

import threading
import time

import numpy as np
import sounddevice as sd

from .devices import Device, is_wasapi, resolve_index
from .util import AudioQueue, StreamResampler

# How long after the last dub sample we still consider the dub "speaking" (feedback guard tail).
SPEAKING_HOLD_S = 0.3
# Never let the original pass-through lag more than this behind real time.
MAX_ORIGINAL_LAG_S = 0.25
# Catch-up: once this much dub is waiting, shorten the pauses inside it (no pitch change, no clicks).
CATCHUP_START_S = 0.5
KEEP_PAUSE_S = 0.08
PAUSE_THRESHOLD = 10 ** (-45 / 20)  # -45 dBFS
PAUSE_FRAME_S = 0.01


class Player:
    def __init__(self, device: Device | None, dub_volume: float = 1.0, original_volume: float = 0.0, duck: float = 0.3):
        index = resolve_index(device.name if device else None, "output")
        info = sd.query_devices(index, "output")
        self.rate = int(info["default_samplerate"])
        self.channels = max(1, min(2, int(info["max_output_channels"])))
        self.dub_volume = dub_volume
        self.original_volume = original_volume
        self.duck = duck

        self._dub = AudioQueue()
        self._orig = AudioQueue()
        self._dub_resamplers: dict[int, StreamResampler] = {}
        self._orig_resampler: StreamResampler | None = None
        self._orig_lock = threading.Lock()
        self._last_dub = 0.0
        self._gain = 1.0
        self._pause_frames = 0
        self.trimmed_s = 0.0

        extra = sd.WasapiSettings(auto_convert=True) if is_wasapi(index) else None
        self._stream = sd.OutputStream(
            device=index,
            samplerate=self.rate,
            channels=self.channels,
            dtype="float32",
            latency="low",
            extra_settings=extra,
            callback=self._callback,
        )

    # -- producer side -------------------------------------------------------------------------
    def play(self, samples: np.ndarray, rate: int) -> None:
        """Queue dubbed speech. Called from the engine (single) thread."""
        rs = self._dub_resamplers.get(rate)
        if rs is None:
            rs = self._dub_resamplers[rate] = StreamResampler(rate, self.rate)
        self._dub.push(self._trim_pauses(rs(samples)))

    def _trim_pauses(self, x: np.ndarray) -> np.ndarray:
        """While behind, keep at most KEEP_PAUSE_S of every silent stretch in the dub."""
        if self.backlog < CATCHUP_START_S:
            self._pause_frames = 0
            return x
        n = int(self.rate * PAUSE_FRAME_S)
        usable = len(x) - len(x) % n
        if usable == 0:
            return x
        frames = x[:usable].reshape(-1, n)
        quiet = np.sqrt(np.mean(np.square(frames), axis=1)) < PAUSE_THRESHOLD
        keep = np.ones(len(frames), dtype=bool)
        max_run = int(KEEP_PAUSE_S / PAUSE_FRAME_S)
        run = self._pause_frames
        for i, q in enumerate(quiet):
            run = run + 1 if q else 0
            keep[i] = run <= max_run
        self._pause_frames = run
        self.trimmed_s += (len(frames) - int(keep.sum())) * PAUSE_FRAME_S
        return np.concatenate([frames[keep].ravel(), x[usable:]])

    def feed_original(self, samples: np.ndarray, rate: int) -> None:
        """Pass the captured source through (called from the capture thread)."""
        if self.original_volume <= 0.0:
            if len(self._orig):
                self._orig.clear()
            return
        with self._orig_lock:
            if self._orig_resampler is None or self._orig_resampler.in_rate != rate:
                self._orig_resampler = StreamResampler(rate, self.rate)
            self._orig.push(self._orig_resampler(samples))
        excess = len(self._orig) - int(self.rate * MAX_ORIGINAL_LAG_S)
        if excess > 0:
            self._orig.drop_oldest(excess)

    def clear(self) -> None:
        self._dub.clear()

    @property
    def backlog(self) -> float:
        """Seconds of dubbed audio waiting to be played."""
        return len(self._dub) / self.rate

    @property
    def speaking(self) -> bool:
        return len(self._dub) > 0 or (time.monotonic() - self._last_dub) < SPEAKING_HOLD_S

    # -- audio thread ---------------------------------------------------------------------------
    def _callback(self, outdata, frames, time_info, status) -> None:
        dub, n = self._dub.pull(frames)
        if n:
            self._last_dub = time.monotonic()
        mix = dub * self.dub_volume
        if self.original_volume > 0.0:
            orig, _ = self._orig.pull(frames)
            target = self.duck if self.speaking else 1.0
            # Fade the ducking gain over several blocks to avoid clicks and pumping.
            new_gain = self._gain + float(np.clip(target - self._gain, -0.08, 0.08))
            ramp = np.linspace(self._gain, new_gain, frames, dtype=np.float32)
            self._gain = new_gain
            mix += orig * ramp * self.original_volume
        np.clip(mix, -1.0, 1.0, out=mix)
        outdata[:] = mix[:, None]

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        try:
            self._stream.stop()
        finally:
            self._stream.close()
