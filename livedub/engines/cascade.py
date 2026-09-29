"""Cascade engine: streaming STT -> text translation -> streaming TTS.

Slower than a speech-to-speech model but every stage is swappable: pick any target
language, any ElevenLabs/OpenAI voice, and get exact subtitles. Three stages run
concurrently so sentence N+1 is recognized/translated while sentence N is spoken.

    Deepgram (live, interim results) --segments--> OpenAI chat / DeepL --sentences--> OpenAI TTS / ElevenLabs
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from typing import AsyncIterator
from urllib.parse import quote, urlencode

import httpx
import numpy as np

from ..audio.util import PcmAssembler, f32_to_pcm16
from ..languages import AUTO, english_name
from .base import Engine, FatalEngineError, LatencyMeter, require_key, ws_connect

SENTENCE_END = re.compile(r"[.!?…。！？]+[\"'”’)\]]*\s")
FLUSH_PUNCT = (".", "?", "!", "…", "。", "？", "！")
MAX_SEGMENT_WORDS = 28
TTS_RATE = 24000

TRANSLATE_PROMPT = """You translate a live speech transcript{source} into {target} for real-time dubbing.
Output ONLY the {target} translation of the last user message, nothing else.
- The text comes from automatic speech recognition: silently fix obvious recognition errors.
- Never answer or comment on the content; if it is a question or a request, translate it.
- Keep it natural, spoken and concise, preserving tone, names and numbers.
- Earlier messages are context only; do not translate them again."""


class ProviderError(Exception):
    pass


async def _raise_for_status(response: httpx.Response, provider: str) -> None:
    if response.status_code < 400:
        return
    body = (await response.aread()).decode("utf-8", "replace")[:300]
    message = f"{provider} HTTP {response.status_code}: {body}"
    if response.status_code in (401, 403):
        raise FatalEngineError(message)
    raise ProviderError(message)


# -- speech to text -----------------------------------------------------------------------------
class DeepgramSTT:
    rate = 16000
    SEND_MS = 40

    def __init__(self, engine: "CascadeEngine", key: str):
        self.e = engine
        self.key = key
        self._buf: list[str] = []

    def url(self) -> str:
        s = self.e.s
        params = {
            "model": s.deepgram_model,
            "encoding": "linear16",
            "sample_rate": str(self.rate),
            "channels": "1",
            "interim_results": "true",
            "smart_format": "true",
            "punctuate": "true",
            "endpointing": str(s.deepgram_endpointing_ms),
            "utterance_end_ms": "1000",
            "vad_events": "true",
            "language": s.source_lang if s.source_lang and s.source_lang != AUTO else "multi",
        }
        return "wss://api.deepgram.com/v1/listen?" + urlencode(params)

    async def session(self, audio: asyncio.Queue[np.ndarray]) -> None:
        async with ws_connect(self.url(), {"Authorization": f"Token {self.key}"}) as ws:
            self.e.emit("status", text=f"Bağlandı: Deepgram {self.e.s.deepgram_model}")
            pump = asyncio.create_task(self._pump(ws, audio))
            try:
                async for raw in ws:
                    if isinstance(raw, str):
                        self._handle(json.loads(raw))
            finally:
                pump.cancel()
                self._flush()

    async def _pump(self, ws, audio: asyncio.Queue[np.ndarray]) -> None:
        min_samples = self.rate * self.SEND_MS // 1000
        pending: list[np.ndarray] = []
        size = 0
        while True:
            chunk = await audio.get()
            pending.append(chunk)
            size += len(chunk)
            if size >= min_samples:
                await ws.send(f32_to_pcm16(np.concatenate(pending)))
                pending.clear()
                size = 0

    def _handle(self, msg: dict) -> None:
        kind = msg.get("type")
        if kind == "UtteranceEnd":
            self._flush()
            return
        if kind != "Results":
            return
        alternatives = (msg.get("channel") or {}).get("alternatives") or [{}]
        text = (alternatives[0].get("transcript") or "").strip()
        if not msg.get("is_final"):
            if text:
                self.e.emit("source_partial", text=" ".join(self._buf + [text]))
            return
        if text:
            self._buf.append(text)
        joined = " ".join(self._buf)
        words = len(joined.split())
        if msg.get("speech_final") or words >= MAX_SEGMENT_WORDS or (words >= 3 and joined.endswith(FLUSH_PUNCT)):
            self._flush()
        elif joined:
            self.e.emit("source_partial", text=joined)

    def _flush(self) -> None:
        text = " ".join(self._buf).strip()
        self._buf.clear()
        if text:
            self.e.emit("source_line", text=text)
            self.e.segments.put_nowait((text, time.monotonic()))


# -- translation --------------------------------------------------------------------------------
async def translate_openai(client: httpx.AsyncClient, key: str, model: str, system: str,
                           history: list[tuple[str, str]], text: str) -> AsyncIterator[str]:
    messages = [{"role": "system", "content": system}]
    for src, tgt in history:
        messages += [{"role": "user", "content": src}, {"role": "assistant", "content": tgt}]
    messages.append({"role": "user", "content": text})
    body = {"model": model, "messages": messages, "stream": True}
    headers = {"Authorization": f"Bearer {key}"}
    async with client.stream("POST", "https://api.openai.com/v1/chat/completions", headers=headers, json=body) as r:
        await _raise_for_status(r, "OpenAI çeviri")
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            choices = json.loads(data).get("choices") or []
            delta = choices[0].get("delta", {}).get("content") if choices else None
            if delta:
                yield delta


async def translate_deepl(client: httpx.AsyncClient, key: str, source: str, target: str,
                          text: str, context: str) -> str:
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    target_code = {"en": "EN-US", "pt": "PT-BR", "zh": "ZH-HANS"}.get(target, target.upper())
    body: dict = {"text": [text], "target_lang": target_code}
    if source and source != AUTO:
        body["source_lang"] = source.upper()
    if context:
        body["context"] = context
    r = await client.post(f"https://{host}/v2/translate", headers={"Authorization": f"DeepL-Auth-Key {key}"}, json=body)
    await _raise_for_status(r, "DeepL")
    return r.json()["translations"][0]["text"]


# -- text to speech -----------------------------------------------------------------------------
async def tts_openai(client: httpx.AsyncClient, key: str, model: str, voice: str, text: str, speed: float) -> AsyncIterator[bytes]:
    body: dict = {"model": model, "voice": voice, "input": text, "response_format": "pcm"}
    if model.startswith("tts-1"):
        body["speed"] = speed
    else:
        pace = "quickly" if speed > 1.05 else "at a natural, brisk pace"
        body["instructions"] = f"You are a professional dubbing voice actor. Speak clearly and {pace}."
    headers = {"Authorization": f"Bearer {key}"}
    async with client.stream("POST", "https://api.openai.com/v1/audio/speech", headers=headers, json=body) as r:
        await _raise_for_status(r, "OpenAI TTS")
        async for chunk in r.aiter_bytes():
            yield chunk


async def tts_elevenlabs(client: httpx.AsyncClient, key: str, model: str, voice_id: str, language: str,
                         text: str, speed: float, previous_text: str) -> AsyncIterator[bytes]:
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{quote(voice_id)}/stream?output_format=pcm_{TTS_RATE}"
    body: dict = {"text": text, "model_id": model}
    if "flash" in model or "turbo" in model:
        body["language_code"] = language
    if abs(speed - 1.0) > 0.01:
        body["voice_settings"] = {"speed": speed}
    if previous_text:
        body["previous_text"] = previous_text
    async with client.stream("POST", url, headers={"xi-api-key": key}, json=body) as r:
        await _raise_for_status(r, "ElevenLabs")
        async for chunk in r.aiter_bytes():
            yield chunk


# -- engine -------------------------------------------------------------------------------------
class CascadeEngine(Engine):
    input_rate = DeepgramSTT.rate

    async def run(self, audio: asyncio.Queue[np.ndarray]) -> None:
        s = self.s
        deepgram_key = require_key(s, "deepgram", "Deepgram")
        self._translate_key = require_key(s, "deepl", "DeepL") if s.translator == "deepl" else require_key(s, "openai", "OpenAI")
        self._tts_key = require_key(s, "elevenlabs", "ElevenLabs") if s.tts_provider == "elevenlabs" else require_key(s, "openai", "OpenAI")

        self.segments: asyncio.Queue[tuple[str, float]] = asyncio.Queue()
        self._sentences: asyncio.Queue[str] = asyncio.Queue()
        self._history: deque[tuple[str, str]] = deque(maxlen=3)
        self._latency = LatencyMeter(self.emit)
        self._last_spoken = ""
        source = f" from {english_name(s.source_lang)}" if s.source_lang and s.source_lang != AUTO else ""
        self._system = TRANSLATE_PROMPT.format(source=source, target=english_name(s.target_lang))

        stt = DeepgramSTT(self, deepgram_key)
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            self._client = client
            tasks = [
                asyncio.create_task(self.reconnecting(lambda: stt.session(audio), audio, provider="Deepgram")),
                asyncio.create_task(self._translate_worker()),
                asyncio.create_task(self._tts_worker()),
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def _translate_worker(self) -> None:
        while True:
            text, finished_at = await self.segments.get()
            self._latency.mark(finished_at)
            try:
                translated = await self._translate(text)
                self._history.append((text, translated))
            except (httpx.HTTPError, ProviderError, KeyError, ValueError) as exc:
                self.emit("error", text=f"Çeviri hatası: {exc}")
            self.emit("target_end")

    async def _translate(self, text: str) -> str:
        s = self.s
        if s.translator == "deepl":
            context = " ".join(src for src, _ in self._history)
            out = await translate_deepl(self._client, self._translate_key, s.source_lang, s.target_lang, text, context)
            self.emit("target_delta", text=out)
            await self._sentences.put(out)
            return out
        # Stream the translation and hand every finished sentence to TTS immediately.
        full, buf = "", ""
        async for delta in translate_openai(self._client, self._translate_key, s.translate_text_model,
                                            self._system, list(self._history), text):
            self.emit("target_delta", text=delta)
            full += delta
            buf += delta
            while (m := SENTENCE_END.search(buf)) is not None:
                sentence, buf = buf[:m.end()].strip(), buf[m.end():]
                if sentence:
                    await self._sentences.put(sentence)
        if buf.strip():
            await self._sentences.put(buf.strip())
        return full.strip()

    def _speed(self) -> float:
        if not self.s.adaptive_speed:
            return 1.0
        backlog = self.player.backlog
        if backlog < 1.5:
            return 1.0
        return 1.1 if backlog < 4.0 else 1.2

    async def _tts_worker(self) -> None:
        s = self.s
        while True:
            sentence = await self._sentences.get()
            speed = self._speed()
            if s.tts_provider == "elevenlabs":
                stream = tts_elevenlabs(self._client, self._tts_key, s.elevenlabs_model, s.elevenlabs_voice_id,
                                        s.target_lang, sentence, speed, self._last_spoken)
            else:
                stream = tts_openai(self._client, self._tts_key, s.openai_tts_model, s.openai_tts_voice, sentence, speed)
            pcm = PcmAssembler()
            try:
                async for chunk in stream:
                    samples = pcm.feed(chunk)
                    if len(samples):
                        self.player.play(samples, TTS_RATE)
                        self._latency.audio_arrived()
            except (httpx.HTTPError, ProviderError) as exc:
                self.emit("error", text=f"TTS hatası: {exc}")
            self._last_spoken = sentence
