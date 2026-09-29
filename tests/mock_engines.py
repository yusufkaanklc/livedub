"""Offline protocol test: runs every engine against local mock servers (no API keys, no audio devices).

    python tests/mock_engines.py
"""

from __future__ import annotations

import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx
import numpy as np
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from livedub.config import Settings  # noqa: E402
from livedub.engines import cascade, gemini_translate, openai_realtime, openai_translate  # noqa: E402

PORT = 8765
TONE = (np.sin(np.arange(4800) / 24000 * 2 * np.pi * 440) * 8000).astype("<i2").tobytes()  # 200 ms @ 24 kHz


class FakePlayer:
    backlog = 0.0
    speaking = False

    def __init__(self):
        self.samples = 0

    def play(self, x, rate):
        assert rate == 24000
        self.samples += len(x)


# -- mock servers -------------------------------------------------------------------------------
async def translate_server(ws, log):
    appends = 0
    async for raw in ws:
        ev = json.loads(raw)
        log.append(ev["type"])
        if ev["type"] == "session.input_audio_buffer.append":
            appends += 1
            if appends % 10 == 0:
                await ws.send(json.dumps({"type": "session.input_transcript.delta", "delta": "hello "}))
                await ws.send(json.dumps({"type": "session.output_transcript.delta", "delta": "hola "}))
                await ws.send(json.dumps({"type": "session.output_audio.delta", "delta": base64.b64encode(TONE).decode()}))


async def gemini_server(ws, log):
    setup = json.loads(await ws.recv())["setup"]
    log.append(("setup", setup))
    await ws.send(json.dumps({"setupComplete": {}}).encode())  # Gemini uses binary frames
    chunks = 0
    async for raw in ws:
        audio = json.loads(raw)["realtimeInput"]["audio"]
        log.append(("audio", audio["mimeType"], len(base64.b64decode(audio["data"]))))
        chunks += 1
        if chunks % 5 == 0:
            content = {
                "inputTranscription": {"text": "hello "},
                "outputTranscription": {"text": "merhaba "},
                "modelTurn": {"parts": [{"inlineData": {"mimeType": "audio/pcm;rate=24000", "data": base64.b64encode(TONE).decode()}}]},
            }
            await ws.send(json.dumps({"serverContent": content}).encode())
        if chunks == 12:
            await ws.send(json.dumps({"serverContent": {"turnComplete": True}}).encode())
            await ws.send(json.dumps({"goAway": {"timeLeft": "1s"}}).encode())


async def realtime_server(ws, log):
    appends = 0
    n = 0
    async for raw in ws:
        ev = json.loads(raw)
        log.append(ev["type"])
        if ev["type"] == "input_audio_buffer.append":
            appends += 1
            if appends % 8 == 0:  # one short utterance every 8 appends
                n += 1
                await ws.send(json.dumps({"type": "input_audio_buffer.speech_started"}))
                await ws.send(json.dumps({"type": "input_audio_buffer.speech_stopped"}))
                await ws.send(json.dumps({"type": "input_audio_buffer.committed", "item_id": f"u{n}"}))
                await ws.send(json.dumps({"type": "conversation.item.added", "item": {"id": f"u{n}"}}))
                await ws.send(json.dumps({"type": "conversation.item.input_audio_transcription.completed", "transcript": f"sentence {n}"}))
        elif ev["type"] == "response.create":
            n += 1
            await ws.send(json.dumps({"type": "response.created", "response": {"id": f"r{n}"}}))
            await ws.send(json.dumps({"type": "conversation.item.added", "item": {"id": f"a{n}"}}))
            await ws.send(json.dumps({"type": "response.output_audio_transcript.delta", "delta": "cümle "}))
            await ws.send(json.dumps({"type": "response.output_audio.delta", "delta": base64.b64encode(TONE).decode()}))
            await ws.send(json.dumps({"type": "response.output_audio_transcript.done"}))
            await ws.send(json.dumps({"type": "response.done", "response": {"status": "completed"}}))
        elif ev["type"] == "conversation.item.delete":
            await ws.send(json.dumps({"type": "conversation.item.deleted", "item_id": ev["item_id"]}))


async def deepgram_server(ws, log):
    chunks = 0
    async for raw in ws:
        log.append("binary" if isinstance(raw, bytes) else json.loads(raw).get("type"))
        chunks += 1
        if chunks % 12 == 0:
            base = {"type": "Results", "channel": {"alternatives": [{"transcript": "This is a test"}]}}
            await ws.send(json.dumps({**base, "is_final": False}))
            await ws.send(json.dumps({**base, "is_final": True, "speech_final": True}))


def http_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/v1/chat/completions":
        body = json.loads(request.content)
        assert body["stream"] is True and body["messages"][-1]["role"] == "user"
        sse = "".join(f"data: {json.dumps({'choices': [{'delta': {'content': t}}]})}\n\n" for t in ["Bu bir ", "test. ", "İkinci cümle"])
        return httpx.Response(200, content=(sse + "data: [DONE]\n\n").encode())
    if request.url.path == "/v1/audio/speech":
        assert json.loads(request.content)["response_format"] == "pcm"
        return httpx.Response(200, content=TONE + b"\x01")  # odd byte count on purpose
    return httpx.Response(404)


# -- harness ------------------------------------------------------------------------------------
async def feed(queue: asyncio.Queue, rate: int):
    block = (np.random.randn(rate // 50) * 0.05).astype(np.float32)
    while True:
        await queue.put(block)
        await asyncio.sleep(0.02)


async def run_engine(engine_cls, settings, seconds=3.0):
    events: list[tuple[str, dict]] = []
    player = FakePlayer()
    engine = engine_cls(settings, player, lambda kind, **data: events.append((kind, data)))
    queue: asyncio.Queue = asyncio.Queue()
    feeder = asyncio.create_task(feed(queue, engine.input_rate))
    task = asyncio.create_task(engine.run(queue))
    await asyncio.sleep(seconds)
    if task.done():
        task.result()
    task.cancel()
    feeder.cancel()
    await asyncio.gather(task, feeder, return_exceptions=True)
    return events, player


def kinds(events):
    return {k for k, _ in events}


async def main():
    logs: dict[str, list] = {"translate": [], "realtime": [], "deepgram": [], "gemini": []}

    async def router(ws):
        path = ws.request.path
        if path.startswith("/ws/google.ai.generativelanguage"):
            assert ws.request.headers["x-goog-api-key"] == "gm-test"
            await gemini_server(ws, logs["gemini"])
        elif path.startswith("/v1/realtime/translations"):
            await translate_server(ws, logs["translate"])
        elif path.startswith("/v1/realtime"):
            assert ws.request.headers["Authorization"] == "Bearer sk-test"
            await realtime_server(ws, logs["realtime"])
        elif path.startswith("/v1/listen"):
            assert ws.request.headers["Authorization"] == "Token dg-test"
            await deepgram_server(ws, logs["deepgram"])

    gemini_translate.URL = f"ws://127.0.0.1:{PORT}/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
    openai_translate.URL = f"ws://127.0.0.1:{PORT}/v1/realtime/translations?model={{model}}"
    openai_realtime.URL = f"ws://127.0.0.1:{PORT}/v1/realtime?model={{model}}"
    cascade.DeepgramSTT.url = lambda self: f"ws://127.0.0.1:{PORT}/v1/listen?model=nova-3"
    real_client = httpx.AsyncClient
    cascade.httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(http_handler), **kw)

    settings = Settings(openai_api_key="sk-test", deepgram_api_key="dg-test", target_lang="es")
    failures = []

    def check(name, cond):
        print(("  OK   " if cond else "  FAIL ") + name)
        if not cond:
            failures.append(name)

    async with serve(router, "127.0.0.1", PORT):
        print("gemini engine")
        ev, player = await run_engine(gemini_translate.GeminiTranslateEngine,
                                      Settings(gemini_api_key="gm-test", target_lang="tr"), seconds=4.0)
        setups = [e[1] for e in logs["gemini"] if e[0] == "setup"]
        audio = [e for e in logs["gemini"] if e[0] == "audio"]
        first = setups[0] if setups else {}
        check("setup: model + Turkish target", first.get("model") == "models/gemini-3.5-live-translate-preview"
              and first["generationConfig"]["translationConfig"] == {"targetLanguageCode": "tr", "echoTargetLanguage": False})
        check("setup: transcriptions at setup level", "outputAudioTranscription" in first and "inputAudioTranscription" in first)
        chunk_bytes = 16000 * gemini_translate.SEND_MS // 1000 * 2
        check(f"audio 16 kHz in {gemini_translate.SEND_MS} ms chunks",
              audio and all(a[1] == "audio/pcm;rate=16000" and a[2] == chunk_bytes for a in audio))
        check("dub audio played", player.samples >= 4800 * 2)
        check("transcripts + latency emitted", {"source_delta", "target_delta", "target_end", "latency"} <= kinds(ev))
        check("reconnected after goAway", len(setups) >= 2)
        check("no errors", "error" not in kinds(ev))

        print("translate engine")
        ev, player = await run_engine(openai_translate.OpenAITranslateEngine, settings)
        check("session.update sent first", logs["translate"][:1] == ["session.update"])
        check("audio appended", logs["translate"].count("session.input_audio_buffer.append") > 20)
        check("dub audio played", player.samples >= 4800)
        check("transcripts + latency emitted", {"source_delta", "target_delta", "latency"} <= kinds(ev))

        print("realtime engine")
        ev, player = await run_engine(openai_realtime.OpenAIRealtimeEngine, settings, seconds=4.0)
        log = logs["realtime"]
        check("two session.updates (base + transcription)", log[:2] == ["session.update", "session.update"])
        check("responses requested", log.count("response.create") >= 3)
        check("old items pruned", log.count("conversation.item.delete") >= 1)
        check("dub audio played", player.samples >= 4800 * 3)
        check("source + target text", {"source_line", "target_delta", "target_end", "latency"} <= kinds(ev))
        check("no errors", "error" not in kinds(ev))

        print("cascade engine")
        ev, player = await run_engine(cascade.CascadeEngine, Settings(openai_api_key="sk-test", deepgram_api_key="dg-test", target_lang="tr"))
        check("pcm streamed to deepgram", logs["deepgram"].count("binary") > 20)
        check("segments translated", any(k == "target_delta" for k, _ in ev))
        check("tts audio played (odd byte handled)", player.samples >= 4800 * 2)
        check("no errors", "error" not in kinds(ev), )
        errors = [d for k, d in ev if k == "error"]
        if errors:
            print("   ", errors[:3])

    print("\nALL PASSED" if not failures else f"\n{len(failures)} FAILED")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
