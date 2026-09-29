"""OpenAI Realtime (gpt-realtime-*) prompted as a simultaneous interpreter.

Works for any target language (e.g. Turkish, which gpt-realtime-translate cannot speak).
Turn handling is done client-side so the dub is never cut off:
  * server VAD commits each utterance but does NOT auto-respond or interrupt,
  * we queue one response at a time, in order,
  * very long utterances are force-committed every ``max_turn_s`` seconds,
  * old conversation items are deleted to keep cost and latency flat.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
import uuid
from urllib.parse import quote

import numpy as np

from ..audio.util import f32_to_pcm16, pcm16_to_f32
from ..languages import AUTO, english_name
from .base import Engine, LatencyMeter, error_text, require_key, ws_connect

URL = "wss://api.openai.com/v1/realtime?model={model}"
SEND_MS = 40
KEEP_ITEMS = 6
IGNORED_ERRORS = {"input_audio_buffer_commit_empty", "conversation_already_has_active_response"}

PROMPT = """You are a professional simultaneous interpreter and dubbing voice actor.
{source_hint}Translate everything you hear into {target} and speak ONLY the {target} translation.

Rules:
- You are not a participant in the conversation. Never answer, comment on, greet or react to what is said. If you hear a question or a request, translate it; do not respond to it.
- Translate only the newest speech that has not been translated yet. Never repeat earlier translations.
- Preserve meaning, tone, emotion and register. Keep names, brands, numbers and technical terms accurate.
- Be concise and natural like a professional film dub. Speak fluently at a brisk, steady pace, without filler words.
- If a sentence is cut off, translate what was said so far without guessing the rest.
- If there is no intelligible speech (music, noise, silence), say nothing."""


class OpenAIRealtimeEngine(Engine):
    input_rate = 24000

    async def run(self, audio: asyncio.Queue[np.ndarray]) -> None:
        self._key = require_key(self.s, "openai", "OpenAI")
        self._latency = LatencyMeter(self.emit)
        await self.reconnecting(lambda: self._session(audio), audio)

    # -- session setup --------------------------------------------------------------------------
    def _instructions(self) -> str:
        src = self.s.source_lang
        hint = f"The speaker talks in {english_name(src)}. " if src and src != AUTO else ""
        return PROMPT.format(source_hint=hint, target=english_name(self.s.target_lang))

    def _config(self, with_transcription: bool) -> dict:
        s = self.s
        turn: dict = {"type": s.vad_type, "create_response": False, "interrupt_response": False}
        if s.vad_type == "server_vad":
            turn.update(threshold=s.vad_threshold, prefix_padding_ms=300, silence_duration_ms=s.vad_silence_ms)
        else:
            turn["eagerness"] = "high"
        audio_in: dict = {"format": {"type": "audio/pcm", "rate": 24000}, "turn_detection": turn}
        if s.noise_reduction:
            audio_in["noise_reduction"] = {"type": s.noise_reduction}
        if with_transcription:
            transcription = {"model": s.realtime_transcribe_model}
            if s.source_lang and s.source_lang != AUTO:
                transcription["language"] = s.source_lang
            audio_in["transcription"] = transcription
        session: dict = {
            "type": "realtime",
            "model": s.realtime_model,
            "instructions": self._instructions(),
            "output_modalities": ["audio"],
            "audio": {
                "input": audio_in,
                "output": {"format": {"type": "audio/pcm", "rate": 24000}, "voice": s.realtime_voice, "speed": s.realtime_speed},
            },
        }
        if s.realtime_reasoning:
            session["reasoning"] = {"effort": s.realtime_reasoning}
        return {"type": "session.update", "session": session}

    async def _session(self, audio: asyncio.Queue[np.ndarray]) -> None:
        self._items: list[str] = []
        self._pending = 0
        self._active = False
        self._response_event: str | None = None
        self._speech_since: float | None = None

        url = URL.format(model=quote(self.s.realtime_model))
        async with ws_connect(url, {"Authorization": f"Bearer {self._key}"}) as ws:
            self._ws = ws
            await ws.send(json.dumps(self._config(False)))
            if self.s.show_source_text and self.s.realtime_transcribe_model:
                # Separate update: if the transcription model is rejected, dubbing keeps working.
                await ws.send(json.dumps(self._config(True)))
            self.emit("status", text=f"Bağlandı: {self.s.realtime_model} → {english_name(self.s.target_lang)}")
            pump = asyncio.create_task(self._pump(audio))
            try:
                async for raw in ws:
                    await self._handle(json.loads(raw))
            finally:
                pump.cancel()

    # -- audio upstream -------------------------------------------------------------------------
    async def _pump(self, audio: asyncio.Queue[np.ndarray]) -> None:
        min_samples = self.input_rate * SEND_MS // 1000
        pending: list[np.ndarray] = []
        size = 0
        while True:
            chunk = await audio.get()
            pending.append(chunk)
            size += len(chunk)
            if size < min_samples:
                continue
            payload = base64.b64encode(f32_to_pcm16(np.concatenate(pending))).decode("ascii")
            pending.clear()
            size = 0
            await self._ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": payload}))

            # Continuous speech (videos, streams) may never pause long enough for the VAD:
            # cut it into chunks so the dub does not fall far behind.
            now = time.monotonic()
            if self._speech_since is not None and now - self._speech_since > self.s.max_turn_s:
                self._speech_since = now
                self._latency.mark(now)
                await self._ws.send(json.dumps({"type": "input_audio_buffer.commit"}))

    # -- server events --------------------------------------------------------------------------
    async def _handle(self, ev: dict) -> None:
        kind = ev.get("type", "")
        if kind in ("response.output_audio.delta", "response.audio.delta"):
            self.player.play(pcm16_to_f32(base64.b64decode(ev["delta"])), 24000)
            self._latency.audio_arrived()
        elif kind in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
            self.emit("target_delta", text=ev.get("delta", ""))
        elif kind in ("response.output_audio_transcript.done", "response.audio_transcript.done"):
            self.emit("target_end")
        elif kind == "input_audio_buffer.speech_started":
            self._speech_since = time.monotonic()
        elif kind == "input_audio_buffer.speech_stopped":
            self._speech_since = None
            self._latency.mark()
        elif kind == "input_audio_buffer.committed":
            self._pending += 1
            await self._respond()
        elif kind in ("conversation.item.added", "conversation.item.created"):
            item_id = (ev.get("item") or {}).get("id")
            if item_id and item_id not in self._items:
                self._items.append(item_id)
        elif kind == "conversation.item.deleted":
            if ev.get("item_id") in self._items:
                self._items.remove(ev["item_id"])
        elif kind == "conversation.item.input_audio_transcription.completed":
            text = (ev.get("transcript") or "").strip()
            if text:
                self.emit("source_line", text=text)
        elif kind == "response.created":
            self._active = True
        elif kind == "response.done":
            self._active = False
            response = ev.get("response") or {}
            if response.get("status") == "failed":
                details = response.get("status_details") or {}
                self.emit("error", text=f"Yanıt başarısız: {error_text(details) if details.get('error') else details}")
            await self._prune()
            await self._respond()
        elif kind == "error":
            err = ev.get("error") or {}
            if err.get("event_id") and err.get("event_id") == self._response_event:
                # Our response.create was rejected: keep the utterance queued for the next attempt.
                self._pending += 1
                if err.get("code") != "conversation_already_has_active_response":
                    self._active = False
            if err.get("code") not in IGNORED_ERRORS:
                self.emit("error", text=error_text(ev))

    async def _respond(self) -> None:
        if self._active or self._pending == 0:
            return
        self._pending = 0
        self._active = True
        self._response_event = f"evt_{uuid.uuid4().hex[:16]}"
        await self._ws.send(json.dumps({"type": "response.create", "event_id": self._response_event}))

    async def _prune(self) -> None:
        excess = len(self._items) - KEEP_ITEMS
        if excess <= 0:
            return
        old, self._items = self._items[:excess], self._items[excess:]
        for item_id in old:
            await self._ws.send(json.dumps({"type": "conversation.item.delete", "item_id": item_id}))
