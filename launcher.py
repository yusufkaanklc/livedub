"""Entry point for PyInstaller builds (package-relative imports need a top-level script)."""

import sys

from livedub.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
