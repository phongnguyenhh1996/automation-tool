"""Entry point for local runs: python main.py ..."""

import sys
from pathlib import Path

_root = Path(__file__).resolve().parent
_src = _root / "src"
if str(_src) not in sys.path:
    sys.path.insert(0, str(_src))

from automation_tool.cli import main

if __name__ == "__main__":
    main()
