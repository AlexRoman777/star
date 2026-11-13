from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


@dataclass(frozen=True, slots=True)
class HostPowerStatus:
    power_source: str
    battery_percent: int | None
    charging: bool | None
    charger_wattage: int | None
    host_load: dict[str, float]


class HostPowerError(RuntimeError):
    pass


class HttpHostPowerProvider:
    """Narrow client for the native Apple power-status service.

    The Mac runs a small launchd service (``apple-power-service.py``) that
    reports AC/battery state, charge level, charger wattage, and host load over
    a token-protected loopback endpoint. The containerized worker cannot read
    ``pmset`` itself, so this is how ``--require-ac-power`` and the battery
    thresholds learn the truth. It replaced the Apple Vision adapter, which used
    to carry the same fields in its health response, in v209 round 3.
    """

    def __init__(
        self,
        *,
        base_url: str,
        allowed_hosts: set[str],
        api_key: str,
        timeout_seconds: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        parsed = urlsplit(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in allowed_hosts
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("power status endpoint must be an explicitly allowed local host")
        token = api_key.strip()
        if not 32 <= len(token) <= 4096 or "\n" in token or "\r" in token:
            raise ValueError("power status service token is invalid")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def health(self) -> bool:
        try:
            response = await self._client.get("/health", timeout=10.0)
            payload = response.json()
            return (
                response.status_code == 200
                and payload.get("status") == "ready"
                and isinstance(payload.get("power"), dict)
            )
        except httpx.HTTPError, json.JSONDecodeError, AttributeError:
            return False

    async def power_status(self) -> HostPowerStatus:
        try:
            response = await self._client.get("/health", timeout=10.0)
            response.raise_for_status()
            payload = response.json()
            power = payload.get("power")
            if not isinstance(power, dict):
                raise ValueError("native adapter did not return host power status")
            power_source = power.get("source")
            battery_percent = power.get("battery_percent")
            charging = power.get("charging")
            charger_wattage = power.get("charger_wattage")
            host_load = power.get("host_load", {})
            if power_source not in {"ac", "battery", "unknown"}:
                raise ValueError("native adapter returned an invalid power source")
            if battery_percent is not None and (
                not isinstance(battery_percent, int)
                or isinstance(battery_percent, bool)
                or not 0 <= battery_percent <= 100
            ):
                raise ValueError("native adapter returned an invalid battery percentage")
            if charging is not None and not isinstance(charging, bool):
                raise ValueError("native adapter returned an invalid charging state")
            if charger_wattage is not None and (
                not isinstance(charger_wattage, int)
                or isinstance(charger_wattage, bool)
                or not 1 <= charger_wattage <= 500
            ):
                raise ValueError("native adapter returned an invalid charger wattage")
            if not isinstance(host_load, dict) or any(
                not isinstance(key, str)
                or not isinstance(value, int | float)
                or isinstance(value, bool)
                or not -1_000_000 <= float(value) <= 10**16
                for key, value in host_load.items()
            ):
                raise ValueError("native adapter returned invalid host load metrics")
            return HostPowerStatus(
                power_source=power_source,
                battery_percent=battery_percent,
                charging=charging,
                charger_wattage=charger_wattage,
                host_load={key: float(value) for key, value in host_load.items()},
            )
        except (httpx.HTTPError, json.JSONDecodeError, AttributeError, ValueError) as exc:
            raise HostPowerError("local host power status is unavailable") from exc
