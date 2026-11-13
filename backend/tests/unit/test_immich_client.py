from __future__ import annotations

import json

import httpx
import pytest

from star.immich.client import ImmichApiError, ImmichClient


async def test_rating_write_uses_bulk_contract_and_rejects_zero() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    async with ImmichClient(
        "https://immich.home",
        "write-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.update_rating("asset-1", 4)
        try:
            await client.update_rating("asset-1", 0)
        except ValueError as exc:
            assert "between 1 and 5" in str(exc)
        else:
            raise AssertionError("zero rating must never be sent to Immich")
    assert len(requests) == 1
    assert requests[0].method == "PATCH"
    assert requests[0].url.path == "/api/assets"
    assert json.loads(requests[0].content) == {"ids": ["asset-1"], "rating": 4}


async def test_rate_limited_write_preserves_retry_after_without_method_fallback():
    requests = []

    async def handler(request):
        requests.append(request.method)
        return httpx.Response(429, headers={"Retry-After": "60"})

    async with ImmichClient(
        "https://immich.home", "test", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(ImmichApiError) as failure:
            await client.update_rating("asset", 5)
    assert failure.value.retry_after_seconds == 60
    assert requests == ["PATCH"]


@pytest.mark.parametrize("forbidden_size", ["original"])
async def test_forbidden_media_sizes_are_rejected_before_network(
    forbidden_size: str,
) -> None:
    network_calls = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal network_calls
        network_calls += 1
        return httpx.Response(500)

    async with ImmichClient(
        "https://immich.home",
        "view-only-key",
        transport=httpx.MockTransport(handler),
    ) as client:
        with pytest.raises(ValueError, match="thumbnail, preview, and direct fullsize"):
            await client.get_thumbnail("asset-1", size=forbidden_size)  # type: ignore[arg-type]
    assert network_calls == 0
