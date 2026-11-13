from __future__ import annotations

from datetime import timedelta

from httpx import ASGITransport, AsyncClient

from star.db.base import utc_now
from star.db.models import (
    ServiceHeartbeat,
    Worker,
)
from star.main import app


async def test_system_overview_derives_online_state_from_heartbeats(
    authenticated_context,
    session_factory,
) -> None:
    async with session_factory() as session:
        session.add_all(
            [
                ServiceHeartbeat(
                    service_key="scheduler",
                    hostname_label="test-coordinator",
                    role="orchestrator",
                    status="online",
                    software_version="test",
                    details={"cycle": "idle"},
                ),
                Worker(
                    token_hash="a" * 64,
                    hostname_label="linux-nvidia",
                    architecture="amd64",
                    operating_system="Ubuntu",
                    software_version="test",
                    capabilities={"cuda": True, "small-vlm": True},
                    runtime_status={"temperature_c": 61.0},
                    allowed_capabilities=["cuda", "small-vlm"],
                    state="active",
                    last_heartbeat=utc_now(),
                ),
                Worker(
                    token_hash="b" * 64,
                    hostname_label="sleeping-mac",
                    architecture="arm64",
                    operating_system="macOS",
                    software_version="test",
                    capabilities={"mlx": True},
                    runtime_status={"power_state": "battery"},
                    allowed_capabilities=["mlx"],
                    state="active",
                    last_heartbeat=utc_now() - timedelta(minutes=5),
                ),
            ]
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as client:
        response = await client.get(
            "/api/v1/system/overview",
            headers={"X-Star-Edge": "star-edge"},
        )

    assert response.status_code == 200, response.text
    payload = response.json()
    component_states = {item["key"]: item["status"] for item in payload["components"]}
    machine_states = {item["hostname_label"]: item["status"] for item in payload["machines"]}
    assert component_states["edge"] == "online"
    assert component_states["scheduler"] == "online"
    assert "prometheus" not in component_states
    assert "grafana" not in component_states
    assert machine_states["linux-nvidia"] == "active"
    assert machine_states["sleeping-mac"] == "offline"
