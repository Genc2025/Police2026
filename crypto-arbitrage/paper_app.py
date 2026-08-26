from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def terminate(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the paper arbitrage scanner and dashboard together")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    scanner_cmd = [sys.executable, str(ROOT / "scanner.py")]
    dashboard_cmd = [
        sys.executable,
        str(ROOT / "dashboard_server.py"),
        "--host",
        args.host,
        "--port",
        str(args.port),
    ]

    print("Starting Crypto Arbitrage PAPER app")
    print("- scanner: continuous public-market scan")
    print(f"- dashboard: http://{args.host}:{args.port}")
    print("- real orders: DISABLED")

    scanner: subprocess.Popen | None = None
    dashboard: subprocess.Popen | None = None

    def stop(_signum=None, _frame=None):
        terminate(scanner)
        terminate(dashboard)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    try:
        scanner = subprocess.Popen(scanner_cmd, cwd=ROOT)
        dashboard = subprocess.Popen(dashboard_cmd, cwd=ROOT)

        while True:
            scanner_code = scanner.poll()
            dashboard_code = dashboard.poll()
            if scanner_code is not None:
                print(f"Scanner exited with code {scanner_code}")
                return scanner_code
            if dashboard_code is not None:
                print(f"Dashboard exited with code {dashboard_code}")
                return dashboard_code
            time.sleep(1)
    except KeyboardInterrupt:
        return 130
    finally:
        stop()


if __name__ == "__main__":
    raise SystemExit(main())
