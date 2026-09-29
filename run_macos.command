#!/bin/bash
# Double-click in Finder (after: chmod +x run_macos.command) or run from Terminal.
cd "$(dirname "$0")" || exit 1

if [ ! -x .venv/bin/python ]; then
    if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
        echo "Python 3.10+ gerekli. https://www.python.org/downloads/ ya da: brew install python"
        exit 1
    fi
    echo "Sanal ortam oluşturuluyor..."
    python3 -m venv .venv || exit 1
fi

.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt || exit 1
exec .venv/bin/python -m livedub "$@"
