from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="livedub", description="Canlı ses çevirisi ve dublaj")
    parser.add_argument("--list-devices", action="store_true", help="ses cihazlarını listele")
    parser.add_argument("--headless", action="store_true", help="arayüz olmadan terminalde çalıştır")
    parser.add_argument("--test-audio", action="store_true", help="API kullanmadan cihazları test et (bip + giriş seviyesi)")
    parser.add_argument("--engine", choices=["gemini", "translate", "realtime", "cascade"])
    parser.add_argument("--source", help="kaynak cihaz id'si (--list-devices çıktısından)")
    parser.add_argument("--output", help="çıkış cihazı id'si")
    parser.add_argument("--source-lang", help="kaynak dil kodu veya 'auto'")
    parser.add_argument("--target", help="hedef dil kodu, ör. tr, en")
    args = parser.parse_args()

    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if args.list_devices:
        from .cli import list_devices

        list_devices()
        return 0
    if args.headless or args.test_audio:
        from .cli import run

        return run(args)

    from .gui import run_gui

    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
