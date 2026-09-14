from __future__ import annotations

import sys
from pathlib import Path

vendor = Path(__file__).resolve().parent.parent / "vendor"
if vendor.exists():
    sys.path.append(str(vendor))
