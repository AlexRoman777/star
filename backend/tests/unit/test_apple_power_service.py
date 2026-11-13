from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[3]
SERVICE_PATH = ROOT / "Deployment/worker-apple/apple-power-service.py"


def test_service_source_compiles_without_using_cached_bytecode() -> None:
    source = SERVICE_PATH.read_text(encoding="utf-8")
    compile(source, str(SERVICE_PATH), "exec")
    # The native LaunchAgent uses macOS /usr/bin/python3, which may predate
    # Python 3.14's parenthesis-free multiple-exception syntax.
    assert re.search(r"^\s*except\s+[^(:]+,", source, re.MULTILINE) is None


def _service_module():
    spec = importlib.util.spec_from_file_location("star_apple_power_service", SERVICE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_power_status_parses_normal_pmset_percentage(monkeypatch) -> None:
    service = _service_module()
    monkeypatch.setattr(service, "_charger_wattage", lambda: 45)
    monkeypatch.setattr(service, "_host_load", lambda: {"load_percent": 25.0})
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=(
                "Now drawing from 'AC Power'\n"
                " -InternalBattery-0 (id=1)\t70%; charging; 1:19 remaining present: true\n"
            )
        ),
    )

    assert service._power_status() == {
        "source": "ac",
        "battery_percent": 70,
        "charging": True,
        "charger_wattage": 45,
        "host_load": {"load_percent": 25.0},
    }


def test_charger_wattage_parser_finds_nested_negotiated_power() -> None:
    service = _service_module()

    assert (
        service._charger_wattage_from_payload(
            {
                "SPPowerDataType": [
                    {
                        "_name": "sppower_ac_charger_information",
                        "sppower_battery_charger_watts": 45,
                    }
                ]
            }
        )
        == 45
    )
    assert service._charger_wattage_from_payload({"Wattage": 0}) is None


def test_discharging_is_not_mistaken_for_charging(monkeypatch) -> None:
    service = _service_module()
    monkeypatch.setattr(service, "_host_load", lambda: {})
    monkeypatch.setattr(service, "_charger_wattage", lambda: 45)
    for source, state in (
        ("Battery", "discharging"),
        ("AC", "not charging"),
        ("AC", "discharging"),
    ):
        monkeypatch.setattr(
            service.subprocess,
            "run",
            lambda *a, source=source, state=state, **kw: SimpleNamespace(
                stdout=f"Now drawing from '{source} Power'\n"
                f" -InternalBattery-0 38%; {state}; present: true"
            ),
        )
        assert service._power_status()["charging"] is False
