"""Keep AP01 display services alive after a reboot / network race.

Why this exists
---------------
On Windows, logon-triggered services can start *before* Wi-Fi has obtained an
address. If a display service binds a fixed LAN IP at that moment it fails with
WinError 10049, the scheduled task exits, and both screens show "data expired".

Two fixes are needed together:

1. The display services bind ``0.0.0.0`` instead of a fixed LAN address.
2. This watchdog checks the local ports once a minute and restarts the matching
   scheduled task when a port disappears.

It deliberately probes ``127.0.0.1``: a short router outage must NOT be
mistaken for a crashed local service.

Usage (run with pythonw.exe from Task Scheduler, once per minute)::

    pythonw.exe tools/screen_service_watchdog.py \
        --service 18765=AP01-Screen-Mode-A \
        --service 18766=AP01-Screen-Mode-B

Task names are whatever YOU named them in Task Scheduler. Nothing is hardcoded
about a particular machine. Records go to ``logs/watchdog.jsonl`` (gitignored).

This script is Windows-only (schtasks.exe). It never touches the device,
never writes firmware, and never opens a console window.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

AUDIT = Path(__file__).resolve().parents[1] / "logs" / "watchdog.jsonl"
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


def port_open(port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def run_task(command: str, name: str) -> None:
    if sys.platform != "win32":
        return
    subprocess.run(
        ["schtasks.exe", command, "/TN", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=NO_WINDOW,
        check=False,
    )


def audit(event: str, port: int, task_name: str) -> None:
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "event": event,
        "port": port,
        "task": task_name,
        "at": datetime.now().astimezone().isoformat(),
    }
    with AUDIT.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")


def parse_services(values: list[str]) -> list[tuple[int, str]]:
    result: list[tuple[int, str]] = []
    for value in values:
        if "=" not in value:
            raise SystemExit(f"bad --service {value!r}, expected PORT=TASKNAME")
        port_text, task_name = value.split("=", 1)
        try:
            port = int(port_text)
        except ValueError:
            raise SystemExit(f"bad port in --service {value!r}")
        if not 1 <= port <= 65535 or not task_name.strip():
            raise SystemExit(f"bad --service {value!r}")
        result.append((port, task_name.strip()))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--service",
        action="append",
        default=[],
        metavar="PORT=TASKNAME",
        help="repeat for each display service (example: 18765=AP01-Screen-Mode-A)",
    )
    parser.add_argument("--settle", type=float, default=15.0, help="seconds to wait after restart")
    args = parser.parse_args()
    services = parse_services(args.service)
    if not services:
        parser.error("at least one --service PORT=TASKNAME is required")

    for port, task_name in services:
        if port_open(port):
            continue
        run_task("/End", task_name)
        time.sleep(2)
        run_task("/Run", task_name)
        time.sleep(args.settle)
        audit("recovered" if port_open(port) else "recovery_failed", port, task_name)


if __name__ == "__main__":
    main()
