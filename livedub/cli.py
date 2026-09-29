"""Headless mode: list devices, test audio, or dub from the terminal."""

from __future__ import annotations

import sys
import threading
from dataclasses import replace

from .audio.devices import default_output, list_outputs, list_sources
from .config import Settings
from .session import DubSession


def list_devices() -> None:
    print("Kaynaklar (--source):")
    for dev in list_sources():
        print(f"  {dev.id!r:60}  {dev.label}")
    print("\nÇıkışlar (--output):")
    default = default_output()
    for dev in list_outputs():
        mark = "  (varsayılan)" if default and dev.name == default.name else ""
        print(f"  {dev.id!r:60}  {dev.name}{mark}")


async def _check_connection() -> int:
    import asyncio
    import ssl

    from websockets.exceptions import ConnectionClosed, InvalidStatus

    from .engines.base import ws_connect
    from .engines.gemini_translate import URL as GEMINI_URL

    targets = [
        ("Gemini", GEMINI_URL, {"x-goog-api-key": "connection-check"}),
        ("OpenAI", "wss://api.openai.com/v1/realtime?model=gpt-realtime-1.5", {"Authorization": "Bearer connection-check"}),
    ]
    failed = False
    for name, url, headers in targets:
        try:
            async with ws_connect(url, headers) as ws:
                await ws.send('{"setup": {"model": "models/connection-check"}}')
                await asyncio.wait_for(ws.recv(), 10)
            result = "güvenli bağlantı kuruldu"
        except (ConnectionClosed, InvalidStatus):
            result = "güvenli bağlantı kuruldu (deneme anahtarı beklendiği gibi reddedildi)"
        except ssl.SSLError as exc:
            result, failed = f"SSL HATASI: {exc}", True
        except (OSError, asyncio.TimeoutError) as exc:
            result, failed = f"BAĞLANTI HATASI: {exc or type(exc).__name__}", True
        print(f"{name}: {result}")
    return 1 if failed else 0


def check_connection() -> int:
    import asyncio

    return asyncio.run(_check_connection())


def run(args) -> int:
    settings = Settings.load()
    overrides = {
        "engine": args.engine,
        "source_device": args.source,
        "output_device": args.output,
        "source_lang": args.source_lang,
        "target_lang": args.target,
    }
    settings = replace(settings, **{k: v for k, v in overrides.items() if v is not None})
    done = threading.Event()
    state = {"target_open": False, "code": 0}

    def on_event(kind: str, data: dict) -> None:
        if kind == "target_delta":
            if not state["target_open"]:
                sys.stdout.write("\n>> ")
                state["target_open"] = True
            sys.stdout.write(data["text"])
            sys.stdout.flush()
        elif kind == "target_end":
            state["target_open"] = False
        elif kind == "source_line":
            print(f"\n<< {data['text']}")
        elif kind == "latency":
            print(f"   [gecikme {data['ms']} ms]")
        elif kind in ("status", "error", "notice"):
            print(f"\n[{kind}] {data['text']}")
        elif kind == "fatal":
            print(f"\n[HATA] {data['text']}")
            state["code"] = 1
        elif kind == "level" and args.test_audio:
            bars = int(max(0.0, data["db"] + 60) / 2)
            sys.stdout.write(f"\r   giriş {data['db']:6.1f} dBFS |{'#' * bars:<30}|")
            sys.stdout.flush()
        elif kind == "stopped":
            done.set()

    session = DubSession(settings, on_event, test_mode=args.test_audio)
    session.start()
    if not args.test_audio:
        print("Çalışıyor. Durdurmak için Ctrl+C.")
    try:
        while not done.wait(0.2):
            pass
    except KeyboardInterrupt:
        session.stop()
        done.wait(5)
    print()
    return state["code"]
