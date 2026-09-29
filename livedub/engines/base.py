"""Engine interface and helpers.

An engine consumes mono float32 audio at ``input_rate`` from an asyncio queue and
pushes dubbed speech into the Player. It reports to the UI through ``emit(kind, **data)``:

    status(text)           connection / state message
    source_delta(text)     live transcript of what was heard (append)
    source_partial(text)   interim transcript that replaces the previous interim text
    source_line(text)      a finished transcript line of what was heard
    source_end()           the current live transcript line is finished
    target_delta(text)     live text of the translation being spoken
    target_end()           the current translated line is finished
    latency(ms)            measured delay for the last utterance
    error(text)            non-fatal problem worth showing
"""

from __future__ import annotations

import asyncio
import json
import ssl
import time
from typing import Awaitable, Callable

import certifi
import numpy as np
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ..audio.player import Player
from ..config import Settings

Emit = Callable[..., None]

PERMANENT_CLOSE_REASONS = (
    "model_not_found", "not found", "insufficient_quota", "quota", "resource_exhausted",
    "billing", "permission", "unsupported", "not supported", "invalid_model",
)


class FatalEngineError(Exception):
    """A problem that retrying will not fix (bad key, unknown model ...)."""


class Engine:
    input_rate = 24000

    def __init__(self, settings: Settings, player: Player, emit: Emit):
        self.s = settings
        self.player = player
        self.emit = emit

    async def run(self, audio: asyncio.Queue[np.ndarray]) -> None:
        raise NotImplementedError

    async def reconnecting(self, session: Callable[[], Awaitable[None]], audio: asyncio.Queue[np.ndarray],
                           provider: str = "OpenAI") -> None:
        """Run ``session`` forever, reconnecting after drops (servers also cap session length)."""
        delay = 1.0
        stale = True
        while True:
            if stale:
                drain(audio)  # never translate audio that piled up while we were offline
            started = time.monotonic()
            try:
                await session()
                if time.monotonic() - started > 5:
                    # Planned close (session time limit, goAway): reconnect at once and keep the
                    # audio buffered meanwhile so no speech is lost.
                    self.emit("status", text="Oturum yenileniyor…")
                    stale = False
                    delay = 1.0
                    continue
                self.emit("status", text="Bağlantı kapandı, yeniden bağlanılıyor…")
            except InvalidStatus as exc:
                code = exc.response.status_code
                detail = _error_body(exc)
                if code in (401, 403):
                    raise FatalEngineError(f"{provider} API anahtarı reddedildi (HTTP {code}). {detail}".strip()) from exc
                if code in (400, 404):
                    raise FatalEngineError(f"{provider} isteği reddetti (HTTP {code}); model adı/dil ayarını kontrol edin. {detail}".strip()) from exc
                self.emit("error", text=f"{provider} HTTP {code}: {detail}")
            except ConnectionClosed as exc:
                # OpenAI accepts the handshake and then closes with 3000 + an error code as reason;
                # Gemini closes with 1007/1008 on bad setup or key. Expired sessions also close,
                # so only give up on errors retrying cannot fix.
                code = exc.rcvd.code if exc.rcvd else None
                reason = exc.rcvd.reason if exc.rcvd else ""
                low = reason.lower()
                if "api_key" in low or "api key" in low:
                    raise FatalEngineError(f"{provider} API anahtarı geçersiz ({reason}).") from exc
                if code in (1007, 1008) or any(tag in low for tag in PERMANENT_CLOSE_REASONS):
                    raise FatalEngineError(f"{provider} oturumu reddetti: {reason or code}") from exc
                self.emit("error", text=f"Bağlantı koptu, yeniden bağlanılıyor: {reason or exc}")
            except ssl.SSLCertVerificationError as exc:
                # Retrying cannot fix a trust problem (missing CA roots, or an HTTPS-inspecting
                # antivirus/proxy whose certificate is not trusted).
                raise FatalEngineError(f"{provider} sunucusunun SSL sertifikası doğrulanamadı: {exc.verify_message}. "
                                       "Antivirüs/VPN HTTPS denetimi yapıyorsa kapatmayı deneyin.") from exc
            except (OSError, asyncio.TimeoutError) as exc:
                self.emit("error", text=f"Bağlantı kurulamadı: {exc or type(exc).__name__}")
            stale = True
            if time.monotonic() - started > 30:
                delay = 1.0
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15.0)


def _tls_context() -> ssl.SSLContext:
    # Python on macOS does not read the Keychain: it looks for a CA file at a path baked in on
    # the build machine, which does not exist on users' Macs, so every TLS handshake fails with
    # CERTIFICATE_VERIFY_FAILED. Trust certifi's bundle on top of whatever the system provides.
    context = ssl.create_default_context()
    context.load_verify_locations(certifi.where())
    return context


TLS_CONTEXT = _tls_context()


def ws_connect(url: str, headers: dict[str, str]):
    return connect(url, additional_headers=headers, ssl=TLS_CONTEXT if url.startswith("wss://") else None,
                   max_size=None, open_timeout=15, ping_interval=20, ping_timeout=20)


def drain(queue: asyncio.Queue) -> None:
    while not queue.empty():
        queue.get_nowait()


def require_key(settings: Settings, name: str, label: str) -> str:
    key = settings.key(name)
    if not key:
        raise FatalEngineError(f"{label} API anahtarı eksik. Ayarlar > API anahtarları bölümünden girin.")
    return key


def error_text(event: dict) -> str:
    err = event.get("error") or {}
    if isinstance(err, dict):
        return err.get("message") or err.get("code") or json.dumps(err, ensure_ascii=False)
    return str(err)


def _error_body(exc: InvalidStatus) -> str:
    body = getattr(exc.response, "body", b"") or b""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            err = data.get("error", data)
            return err.get("message", "") if isinstance(err, dict) else str(err)
    except ValueError:
        pass
    return body.decode("utf-8", "replace")[:300]


class LatencyMeter:
    """Measures the time from a reference moment (speech end, onset ...) to the first dubbed audio."""

    def __init__(self, emit: Emit):
        self._emit = emit
        self._start: float | None = None

    def mark(self, t: float | None = None) -> None:
        if self._start is None:
            self._start = time.monotonic() if t is None else t

    def audio_arrived(self) -> None:
        if self._start is not None:
            self._emit("latency", ms=int((time.monotonic() - self._start) * 1000))
            self._start = None


class OnsetDetector:
    """Cheap energy gate used to time speech onsets for the latency readout."""

    def __init__(self, threshold_db: float = -42.0, release_s: float = 0.6):
        self.threshold = 10 ** (threshold_db / 20)
        self.release = release_s
        self._last_loud = 0.0
        self._active = False

    def feed(self, x: np.ndarray) -> bool:
        """Return True when a new speech onset starts in this block."""
        now = time.monotonic()
        loud = len(x) > 0 and float(np.sqrt(np.mean(np.square(x)))) > self.threshold
        onset = loud and not self._active
        if loud:
            self._last_loud = now
            self._active = True
        elif self._active and now - self._last_loud > self.release:
            self._active = False
        return onset
