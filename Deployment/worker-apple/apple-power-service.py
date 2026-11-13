#!/usr/bin/env python3
"""Minimal native Apple power/host-load status service for the Star accelerator worker.

Runs as a launchd user agent on the Apple Silicon Apple using the macOS-shipped
Python (stdlib only). The Docker worker reads ``GET /health`` through
``--power-status-url`` to learn whether the Apple worker is on AC power, the battery
level, the negotiated charger wattage, and bounded host load. Without this
service the worker treats power as unknown and holds all work.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import subprocess
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

RUNTIME_NAME = "apple-power"

_CHARGER_CACHE_SECONDS = 120.0
_charger_cache_lock = threading.Lock()
_charger_cache_updated_at = 0.0
_charger_cache_watts: int | None = None


def _host_load() -> dict[str, float]:
    """Read bounded native Mac load and unified-memory pressure.

    Apple does not expose a stable unprivileged GPU-utilization API. We report
    only measurements macOS can provide truthfully without powermetrics/root.
    """

    values: dict[str, float] = {}
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
        logical_cpus = max(1, os.cpu_count() or 1)
        values.update(
            {
                "logical_cpus": float(logical_cpus),
                "load_1m": float(load_1m),
                "load_5m": float(load_5m),
                "load_15m": float(load_15m),
                "load_percent": min(100.0, load_1m / logical_cpus * 100.0),
            }
        )
    except OSError:
        pass
    try:
        total_result = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "hw.memsize"],
            check=True,
            timeout=3,
            capture_output=True,
            text=True,
        )
        total = int(total_result.stdout.strip())
        vm_result = subprocess.run(
            ["/usr/bin/vm_stat"],
            check=True,
            timeout=3,
            capture_output=True,
            text=True,
        )
        page_match = re.search(r"page size of (\d+) bytes", vm_result.stdout)
        page_size = int(page_match.group(1)) if page_match else 4096
        pages: dict[str, int] = {}
        for line in vm_result.stdout.splitlines():
            if ":" not in line:
                continue
            key, raw = line.split(":", 1)
            number = re.search(r"\d+", raw.replace(".", ""))
            if number:
                pages[key.strip()] = int(number.group())
        available_pages = sum(
            pages.get(key, 0)
            for key in ("Pages free", "Pages inactive", "Pages speculative", "Pages purgeable")
        )
        available = min(total, max(0, available_pages * page_size))
        used = max(0, total - available)
        values.update(
            {
                "memory_total_bytes": float(total),
                "memory_available_bytes": float(available),
                "memory_used_bytes": float(used),
                "memory_percent": used / total * 100.0,
            }
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return values


def _charger_wattage_from_payload(value: Any) -> int | None:
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                "watt" in str(key).lower()
                and isinstance(item, int)
                and not isinstance(item, bool)
                and 1 <= item <= 500
            ):
                return item
            nested = _charger_wattage_from_payload(item)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for item in value:
            nested = _charger_wattage_from_payload(item)
            if nested is not None:
                return nested
    return None


def _charger_wattage() -> int | None:
    """Return negotiated charger watts without running system_profiler every heartbeat."""

    global _charger_cache_updated_at, _charger_cache_watts
    now = time.monotonic()
    with _charger_cache_lock:
        if now - _charger_cache_updated_at < _CHARGER_CACHE_SECONDS:
            return _charger_cache_watts
        try:
            result = subprocess.run(
                ["/usr/sbin/system_profiler", "SPPowerDataType", "-json"],
                check=True,
                timeout=10,
                capture_output=True,
                text=True,
            )
            payload = json.loads(result.stdout)
            watts = _charger_wattage_from_payload(payload)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            watts = None
        _charger_cache_updated_at = now
        _charger_cache_watts = watts
        return watts


def _power_status() -> dict[str, Any]:
    try:
        result = subprocess.run(
            ["/usr/bin/pmset", "-g", "batt"],
            check=True,
            timeout=5,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return {
            "source": "unknown",
            "battery_percent": None,
            "charging": None,
            "charger_wattage": None,
        }
    text = result.stdout
    # ``pmset -g batt`` formats the percentage as ``70%;``.  A trailing
    # word-boundary cannot match between ``%`` and ``;`` because both are
    # non-word characters, which previously made every real Mac battery level
    # appear unknown and held all Apple work indefinitely.
    match = re.search(r"(?<!\d)(\d{1,3})%(?!\d)", text)
    percent = min(100, int(match.group(1))) if match else None
    lowered = text.lower()
    source = (
        "ac" if "ac power" in lowered else "battery" if "battery power" in lowered else "unknown"
    )
    charging = (
        True
        if source == "ac" and re.search(r";\s*charging(?:;|$)", lowered)
        else False
        if percent is not None
        else None
    )
    return {
        "source": source,
        "battery_percent": percent,
        "charging": charging,
        "charger_wattage": _charger_wattage() if source == "ac" else None,
        "host_load": _host_load(),
    }


class PowerHandler(BaseHTTPRequestHandler):
    server: PowerServer

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _authorized(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {self.server.token}"
        return hmac.compare_digest(supplied, expected)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path != "/health":
            self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return
        self._json(
            HTTPStatus.OK,
            {
                "status": "ready",
                "runtime": RUNTIME_NAME,
                "power": _power_status(),
            },
        )

    def do_POST(self) -> None:
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})


class PowerServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], *, token: str) -> None:
        super().__init__(address, PowerHandler)
        self.token = token


def main() -> None:
    parser = argparse.ArgumentParser(description="Star Apple power status service")
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip()
    if not 32 <= len(token) <= 4096:
        raise SystemExit("Apple power token is invalid")
    server = PowerServer((args.host, args.port), token=token)
    server.serve_forever()


if __name__ == "__main__":
    main()
