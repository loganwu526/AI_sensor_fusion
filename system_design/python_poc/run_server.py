"""自動偵測本機可用連接埠並啟動 FastAPI 感測融合戰情室伺服器。"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import uvicorn

CANDIDATE_PORTS: tuple[int, ...] = (8000, 8001, 8080, 8765, 8888, 18000)


def find_available_port(host: str = "127.0.0.1") -> int:
    """依序探測候選連接埠；若皆被佔用則請求作業系統分配可用埠。"""
    for port in CANDIDATE_PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def main() -> None:
    """設定專案路徑並於可用埠啟動 Uvicorn 伺服器。"""
    project_root = Path(__file__).resolve().parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    host = "127.0.0.1"
    port = find_available_port(host)
    print(f"Dashboard URL: http://{host}:{port}/", flush=True)
    print(f"API Docs URL:  http://{host}:{port}/docs", flush=True)

    uvicorn.run("app.main:app", host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
