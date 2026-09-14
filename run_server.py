from __future__ import annotations

import os
import sys
from pathlib import Path

import uvicorn


PROJECT_DIR = Path(__file__).resolve().parent


def main() -> None:
    os.chdir(PROJECT_DIR)
    if str(PROJECT_DIR) not in sys.path:
        sys.path.insert(0, str(PROJECT_DIR))

    # 监听局域网，便于微信开发者工具和同一 Wi-Fi 下的安卓真机访问。
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8003"))
    reload = os.getenv("RELOAD", "false").lower() == "true"

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload,
    )


if __name__ == "__main__":
    main()
