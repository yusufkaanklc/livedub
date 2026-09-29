"""Wires capture -> engine -> player together on a background thread with its own asyncio loop."""

from __future__ import annotations

import asyncio
import threading
import time
import traceback
from typing import Callable

import numpy as np

from .audio.capture import open_capture
from .audio.devices import (
    IS_MAC,
    IS_WINDOWS,
    SYSTEM_LOOPBACK_ID,
    SYSTEM_SOURCE,
    Device,
    com_init,
    com_uninit,
    feedback_risk,
    find_output,
    find_source,
    physical_output,
    process_loopback_supported,
)
from .audio.macos import microphone_permission
from .audio.player import Player
from .audio.util import StreamResampler, rms_dbfs
from .config import Settings
from .engines import ENGINES, FatalEngineError

EventSink = Callable[[str, dict], None]

QUEUE_MAX_BLOCKS = 500  # ~10 s of 20 ms blocks
LEVEL_INTERVAL_S = 0.06
TEST_DURATION_S = 6.0
DEFAULT_MIC = Device("", "default microphone", "input")


SWITCH_NOTE = ("Kaynak ve çıkış aynı cihaz: dublaj geri karışıp çeviriyi döngüye sokmasın diye "
               "'Tüm sistem sesi (dublaj hariç)' yakalanıyor.")

# Warn when the source has delivered nothing but exact digital silence for this long.
SILENCE_WARN_S = 6.0
MAC_PERMISSION_OFF = ("LiveDub'ın mikrofon izni kapalı. macOS bu yüzden BlackHole dahil tüm girişlerden yalnızca "
                      "sessizlik veriyor. Sistem Ayarları › Gizlilik ve Güvenlik › Mikrofon › LiveDub'ı açın ve "
                      "uygulamayı yeniden başlatın.")


def silence_hint(source: Device | None, permission: str | None) -> str:
    if permission in ("denied", "restricted"):
        return MAC_PERMISSION_OFF
    if source is not None and source.kind == "loopback":
        return "Kaynaktan henüz ses gelmiyor. Çevrilecek bir video ya da ses oynatın."
    if source is not None and source.virtual:
        if IS_MAC:
            return (f"{source.name} üzerinden hiç ses gelmiyor. Mac'in ses çıkışını {source.name} yapın "
                    "(Sistem Ayarları › Ses › Çıkış) ve bir şey oynatın. Sürüyorsa: Sistem Ayarları › "
                    "Gizlilik ve Güvenlik › Mikrofon › LiveDub açık olmalı.")
        return (f"{source.name} üzerinden hiç ses gelmiyor. Çevrilecek uygulamanın çıkışını bu sanal kabloya "
                "yönlendirin ve bir şey oynatın.")
    return ("Mikrofondan hiç ses gelmiyor (tam sessizlik). Mikrofon sessize alınmış ya da uygulamanın "
            "mikrofon izni kapalı olabilir.")


def plan_input(mode: str, source: Device | None, output: Device | None) -> tuple[Device | None, bool, str | None]:
    """Choose what to capture and whether to mute it while the dub plays.

    If the dub would leak into a system-audio capture, capture everything except LiveDub
    instead (Windows process loopback): translators loop on their own voice, and muting the
    input while dubbing would cut the source they are still translating.
    """
    risky = feedback_risk(source or DEFAULT_MIC, output)
    if (mode == "auto" and risky and source is not None and source.kind == "loopback"
            and source.id != SYSTEM_LOOPBACK_ID and process_loopback_supported()):
        return SYSTEM_SOURCE, False, SWITCH_NOTE
    return source, mode == "on" or (mode == "auto" and risky), None


class DubSession:
    """One run of the dubbing pipeline. ``test_mode`` only checks devices (tone + input meter), no API calls."""

    def __init__(self, settings: Settings, on_event: EventSink, test_mode: bool = False):
        self.s = settings
        self._on_event = on_event
        self._test_mode = test_mode
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._capture_error: Exception | None = None
        self._ducker = None  # sessions.OriginalDucker when the original is turned down in the mixer
        self.player: Player | None = None

    # -- public API (any thread) ----------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._thread_main, name="dub-session", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        loop, event = self._loop, self._stop_event
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass  # loop already closed

    def join(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def set_volumes(self, dub: float | None = None, original: float | None = None) -> None:
        if self.player is None:
            return
        if dub is not None:
            self.player.dub_volume = dub
        if original is not None:
            if self._ducker is not None:
                self._ducker.level = original  # applied by _duck_loop on the session thread
            else:
                self.player.original_volume = original

    # -- internals ------------------------------------------------------------------------------
    def emit(self, kind: str, **data) -> None:
        try:
            self._on_event(kind, data)
        except Exception:
            traceback.print_exc()

    def _thread_main(self) -> None:
        com_init()
        try:
            asyncio.run(self._main())
        except FatalEngineError as exc:
            self.emit("fatal", text=str(exc))
        except Exception as exc:
            traceback.print_exc()
            self.emit("fatal", text=f"{type(exc).__name__}: {exc}")
        finally:
            self.player = None
            com_uninit()
            self.emit("stopped")

    def _resolve_devices(self) -> tuple[Device | None, Device | None, bool, list[str]]:
        """Returns (source, device to play the dub on or None for the default, guard, notices)."""
        s = self.s
        source = find_source(s.source_device) if s.source_device else None
        if s.source_device and source is None:
            raise FatalEngineError("Seçili kaynak cihaz bulunamadı. Cihaz listesini yenileyin.")
        output = find_output(s.output_device)
        if s.output_device and output is None:
            raise FatalEngineError("Seçili çıkış cihazı bulunamadı. Cihaz listesini yenileyin.")
        notes: list[str] = []
        play_on = output if s.output_device else None
        # Routing system audio into BlackHole / VB-CABLE makes the cable the system default output;
        # playing the dub there would be inaudible and loop straight back into the capture.
        if not s.output_device and output is not None and output.virtual:
            fallback = physical_output()
            if fallback is not None:
                notes.append(f"Varsayılan çıkış bir sanal kablo ({output.name}); dublaj {fallback.name} üzerinden çalınıyor.")
                output = play_on = fallback
        source, guard, note = plan_input(s.feedback_guard, source, output)
        if note:
            notes.append(note)
        return source, play_on, guard, notes

    async def _main(self) -> None:
        s = self.s
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._stop_event = asyncio.Event()

        source, play_on, guard, notes = self._resolve_devices()
        # System-audio sources: the original already plays from its own app, so "original volume"
        # turns those apps down in the Windows mixer instead of mixing a copy into our output.
        ducker = None
        if IS_WINDOWS and source is not None and source.kind == "loopback":
            from .audio.sessions import OriginalDucker, recover

            recover()
            ducker = OriginalDucker(s.original_volume)
            self._ducker = ducker
        player = Player(play_on, s.dub_volume, 0.0 if ducker else s.original_volume, s.duck_level)
        engine = None if self._test_mode else ENGINES[s.engine](s, player, self.emit)
        engine_rate = engine.input_rate if engine else 24000
        audio_q: asyncio.Queue[np.ndarray] = asyncio.Queue(maxsize=QUEUE_MAX_BLOCKS)

        resampler: StreamResampler | None = None
        level = {"at": 0.0, "peak": -120.0}
        activity = {"heard": False}  # set once the source delivers anything but digital silence

        def push(block: np.ndarray) -> None:
            if audio_q.full():
                audio_q.get_nowait()  # drop the oldest block rather than lag behind
            audio_q.put_nowait(block)

        def on_audio(block: np.ndarray, rate: int) -> None:  # capture thread
            nonlocal resampler
            if ducker is not None and ducker.gain != 1.0:
                block = block * ducker.gain  # undo the mixer turn-down for the translator
            now = time.monotonic()
            if not activity["heard"] and len(block) and float(np.max(np.abs(block))) > 1e-6:
                activity["heard"] = True
            level["peak"] = max(level["peak"], rms_dbfs(block))
            if now - level["at"] >= LEVEL_INTERVAL_S:
                self.emit("level", db=level["peak"])
                level["at"], level["peak"] = now, -120.0
            player.feed_original(block, rate)
            if guard and player.speaking:
                block = np.zeros_like(block)  # our own dub is leaking back in: send silence instead
            if resampler is None or resampler.in_rate != rate:
                resampler = StreamResampler(rate, engine_rate)
            out = resampler(block)
            if len(out):
                try:
                    loop.call_soon_threadsafe(push, out)
                except RuntimeError:
                    pass  # shutting down

        def on_capture_error(exc: Exception) -> None:
            self._capture_error = exc
            try:
                loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass

        player.start()
        self.player = player
        duck_task = asyncio.create_task(_duck_loop(ducker)) if ducker else None
        try:
            if ducker is not None:
                ducker.update()  # inside try: the finally below always restores the apps
            capture = open_capture(source, on_audio, on_capture_error)
            capture.start()
            try:
                self.emit("running", guard=guard, output_rate=player.rate, input_rate=capture.rate)
                for text in notes:
                    self.emit("notice", text=text)
                if guard and s.feedback_guard == "auto":
                    self.emit("notice", text="Geri besleme koruması açık: dublaj çalarken giriş susturuluyor.")
                permission = microphone_permission() if IS_MAC else None
                monitor = asyncio.create_task(self._watch_input(source, permission, activity))
                try:
                    await self._run_until_stopped(engine, audio_q, player)
                finally:
                    monitor.cancel()
                    await asyncio.gather(monitor, return_exceptions=True)
            finally:
                capture.stop()
        finally:
            player.stop()
            if duck_task is not None:
                duck_task.cancel()
                await asyncio.gather(duck_task, return_exceptions=True)
            if ducker is not None:
                ducker.restore()
                self._ducker = None
        if self._capture_error is not None:
            raise FatalEngineError(f"Ses yakalama durdu: {self._capture_error}")

    async def _run_until_stopped(self, engine, audio_q: asyncio.Queue[np.ndarray], player: Player) -> None:
        if engine is None:
            player.play(_test_tone(), 24000)
            self.emit("status", text="Ses testi: bip sesi çalınıyor, giriş seviyesini izleyin…")
            main = asyncio.create_task(asyncio.sleep(TEST_DURATION_S))
        else:
            self.emit("status", text="Bağlanıyor…")
            main = asyncio.create_task(engine.run(audio_q))
        stopper = asyncio.create_task(self._stop_event.wait())
        stats = asyncio.create_task(self._report_backlog(player))
        try:
            done, _ = await asyncio.wait({main, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if main in done:
                main.result()  # re-raise engine failures
        finally:
            for task in (main, stopper, stats):
                task.cancel()
            await asyncio.gather(main, stopper, stats, return_exceptions=True)

    async def _watch_input(self, source: Device | None, permission: str | None, activity: dict) -> None:
        """Explain a silent source instead of leaving the user staring at an empty meter.

        macOS answers a missing microphone permission, or a BlackHole nothing is routed into,
        with digital silence rather than an error.
        """
        if permission in ("denied", "restricted"):
            self.emit("input_silent", text=MAC_PERMISSION_OFF)
        started = time.monotonic()
        warned = False
        while not activity["heard"]:
            if not warned and time.monotonic() - started > SILENCE_WARN_S:
                self.emit("input_silent", text=silence_hint(source, permission))
                warned = True
            await asyncio.sleep(0.25)
        if warned or permission in ("denied", "restricted"):
            self.emit("input_ok")

    async def _report_backlog(self, player: Player) -> None:
        while True:
            self.emit("backlog", seconds=player.backlog)
            await asyncio.sleep(0.25)


async def _duck_loop(ducker) -> None:
    # Catch apps that start playing later and slider changes.
    while True:
        await asyncio.sleep(0.25)
        try:
            ducker.update()
        except OSError:
            pass


def _test_tone() -> np.ndarray:
    rate = 24000
    t = np.arange(int(rate * 0.25)) / rate
    beep = 0.25 * np.sin(2 * np.pi * 880 * t) * np.hanning(len(t))
    gap = np.zeros(int(rate * 0.1))
    return np.concatenate([beep, gap, beep * 0.8]).astype(np.float32)
