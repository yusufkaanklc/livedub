"""Google Gemini Live Translate: continuous simultaneous interpretation over the Live API.

Like gpt-realtime-translate it speaks while the speaker is still talking, but it can
output Turkish and 70+ other languages. Note: despite echoTargetLanguage=false, the model
loops badly if its own dub leaks back into the input, so the capture must exclude it.
"""

from __future__ import annotations

import asyncio
import base64
import json

import numpy as np

from ..audio.util import f32_to_pcm16, pcm16_to_f32
from ..languages import display_name
from .base import Engine, FatalEngineError, LatencyMeter, OnsetDetector, require_key, ws_connect

URL = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
# The docs suggest 100 ms chunks; 40 ms measured ~0.1-0.2 s faster first audio with the same output.
SEND_MS = 40
SETUP_TIMEOUT_S = 15
# BCP-47 codes the Live API expects where they differ from our ISO 639-1 codes.
LANGUAGE_CODES = {"zh": "zh-Hans", "pt": "pt-BR"}


class GeminiTranslateEngine(Engine):
    input_rate = 16000

    async def run(self, audio: asyncio.Queue[np.ndarray]) -> None:
        self._key = require_key(self.s, "gemini", "Gemini")
        self._latency = LatencyMeter(self.emit)
        self._onset = OnsetDetector()
        await self.reconnecting(lambda: self._session(audio), audio, provider="Gemini")

    def _setup(self) -> dict:
        target = LANGUAGE_CODES.get(self.s.target_lang, self.s.target_lang)
        setup: dict = {
            "model": f"models/{self.s.gemini_translate_model}",
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "translationConfig": {"targetLanguageCode": target, "echoTargetLanguage": False},
            },
            "outputAudioTranscription": {},
        }
        if self.s.show_source_text:
            setup["inputAudioTranscription"] = {}
        return {"setup": setup}

    async def _session(self, audio: asyncio.Queue[np.ndarray]) -> None:
        async with ws_connect(URL, {"x-goog-api-key": self._key}) as ws:
            await ws.send(json.dumps(self._setup()))

            async def setup_complete() -> None:
                while "setupComplete" not in _decode(await ws.recv()):
                    pass

            try:
                await asyncio.wait_for(setup_complete(), SETUP_TIMEOUT_S)
            except asyncio.TimeoutError as exc:
                raise FatalEngineError("Gemini oturum kurulumunu onaylamadı; model adını kontrol edin.") from exc
            self.emit("status", text=f"Bağlandı: {self.s.gemini_translate_model} → {display_name(self.s.target_lang)}")
            pump = asyncio.create_task(self._pump(ws, audio))
            try:
                async for raw in ws:
                    if self._handle(_decode(raw)):
                        return  # goAway: reconnect right away
            finally:
                pump.cancel()

    async def _pump(self, ws, audio: asyncio.Queue[np.ndarray]) -> None:
        min_samples = self.input_rate * SEND_MS // 1000
        pending: list[np.ndarray] = []
        size = 0
        while True:
            chunk = await audio.get()
            # Only time onsets that start while nothing is being dubbed; otherwise the reading
            # measures leftover audio of the previous sentence.
            if self._onset.feed(chunk) and not self.player.speaking:
                self._latency.mark()
            pending.append(chunk)
            size += len(chunk)
            if size < min_samples:
                continue
            payload = base64.b64encode(f32_to_pcm16(np.concatenate(pending))).decode("ascii")
            pending.clear()
            size = 0
            message = {"realtimeInput": {"audio": {"data": payload, "mimeType": f"audio/pcm;rate={self.input_rate}"}}}
            await ws.send(json.dumps(message))

    def _handle(self, msg: dict) -> bool:
        content = msg.get("serverContent")
        if content:
            for part in (content.get("modelTurn") or {}).get("parts") or []:
                data = (part.get("inlineData") or {}).get("data")
                if data:
                    self.player.play(pcm16_to_f32(base64.b64decode(data)), 24000)
                    self._latency.audio_arrived()
            source = (content.get("inputTranscription") or {}).get("text")
            if source:
                self.emit("source_delta", text=source)
            target = (content.get("outputTranscription") or {}).get("text")
            if target:
                self.emit("target_delta", text=target)
            if content.get("turnComplete"):
                self.emit("source_end")
                self.emit("target_end")
        if "goAway" in msg:
            self.emit("status", text="Gemini oturumu yenileniyor…")
            return True
        return False


def _decode(raw: str | bytes) -> dict:
    # The Live API sends JSON in binary frames.
    return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
