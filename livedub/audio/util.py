"""Small audio helpers shared by capture, playback and engines."""

from __future__ import annotations

import threading
from collections import deque

import numpy as np
import soxr


def to_mono(block: np.ndarray) -> np.ndarray:
    """Down-mix a (frames, channels) block to a new contiguous float32 mono array."""
    if block.ndim == 1:
        return np.array(block, dtype=np.float32)
    return block.mean(axis=1, dtype=np.float32)


def f32_to_pcm16(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def pcm16_to_f32(data: bytes) -> np.ndarray:
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0


def rms_dbfs(x: np.ndarray) -> float:
    if len(x) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(x), dtype=np.float64)))
    return 20.0 * np.log10(rms + 1e-9)


class StreamResampler:
    """Chunk-by-chunk mono resampler that keeps filter state between calls.

    Not thread-safe: use one instance per producer thread.
    """

    def __init__(self, in_rate: int, out_rate: int):
        self.in_rate = int(in_rate)
        self.out_rate = int(out_rate)
        self._rs = None
        if self.in_rate != self.out_rate:
            # LQ holds back ~1-6 ms of audio versus ~15-26 ms for MQ; plenty of quality for speech.
            self._rs = soxr.ResampleStream(self.in_rate, self.out_rate, 1, dtype="float32", quality="LQ")

    def __call__(self, x: np.ndarray) -> np.ndarray:
        x = np.ascontiguousarray(x, dtype=np.float32)
        if self._rs is None:
            return x
        return self._rs.resample_chunk(x)


class PcmAssembler:
    """Turns an arbitrary byte stream of PCM16 into float32 blocks (handles odd splits)."""

    def __init__(self):
        self._rest = b""

    def feed(self, data: bytes) -> np.ndarray:
        data = self._rest + data
        cut = len(data) - (len(data) % 2)
        self._rest = data[cut:]
        return pcm16_to_f32(data[:cut])


class AudioQueue:
    """Thread-safe FIFO of float32 samples used between the asyncio side and the audio callback."""

    def __init__(self):
        self._chunks: deque[np.ndarray] = deque()
        self._head = 0
        self._size = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return self._size

    def push(self, x: np.ndarray) -> None:
        if len(x) == 0:
            return
        with self._lock:
            self._chunks.append(np.asarray(x, dtype=np.float32))
            self._size += len(x)

    def pull(self, n: int) -> tuple[np.ndarray, int]:
        """Return exactly ``n`` samples (zero padded) and how many were real audio."""
        out = np.zeros(n, dtype=np.float32)
        filled = 0
        with self._lock:
            while filled < n and self._chunks:
                chunk = self._chunks[0]
                take = min(len(chunk) - self._head, n - filled)
                out[filled:filled + take] = chunk[self._head:self._head + take]
                filled += take
                self._head += take
                if self._head >= len(chunk):
                    self._chunks.popleft()
                    self._head = 0
            self._size -= filled
        return out, filled

    def drop_oldest(self, n: int) -> None:
        with self._lock:
            while n > 0 and self._chunks:
                chunk = self._chunks[0]
                take = min(len(chunk) - self._head, n)
                self._head += take
                self._size -= take
                n -= take
                if self._head >= len(chunk):
                    self._chunks.popleft()
                    self._head = 0

    def clear(self) -> None:
        with self._lock:
            self._chunks.clear()
            self._head = 0
            self._size = 0
