from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from star.main import app


async def test_dashboard_throughput_reports_unavailable_zero_series_before_snapshot(
    authenticated_context,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as client:
        response = await client.get("/api/v1/dashboard/throughput")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["available"] is False
    assert payload["bucket_seconds"] == 1800
    assert set(payload["series"]) == {"primary", "senior", "highlights"}
    assert all(len(series) == 48 and not any(series) for series in payload["series"].values())


async def test_dashboard_throughput_reads_the_fleet_snapshot(
    authenticated_context,
    session_factory,
) -> None:
    from datetime import UTC, datetime

    from star.db.models import PerformanceSnapshot

    async with session_factory() as session:
        session.add(
            PerformanceSnapshot(
                key="fleet",
                observed_at=datetime.now(UTC),
                payload={
                    "throughput_history": {
                        "bucket_seconds": 1800,
                        "starts_at": "2026-09-06T13:00:00+00:00",
                        "series": {"primary": [0] * 47 + [12], "senior": [0] * 48},
                    }
                },
            )
        )
        await session.commit()

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as client:
        response = await client.get("/api/v1/dashboard/throughput")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["available"] is True
    assert payload["stale"] is False
    assert payload["series"]["primary"][-1] == 12
    assert payload["starts_at"].startswith("2026-09-06T13:00:00")
