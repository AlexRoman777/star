from __future__ import annotations

import uuid

from httpx import ASGITransport, AsyncClient

from star.main import app


async def test_audit_token_is_scoped_media_free_and_revocable(
    authenticated_context,
) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as owner:
        created = await owner.post(
            "/api/v1/audit/tokens",
            json={"name": "external review", "expires_at": None},
        )
        assert created.status_code == 200, created.text
        token_id = created.json()["id"]
        secret = created.json()["token"]
        assert secret.startswith("ppa_")

        listed = await owner.get("/api/v1/audit/tokens")
        assert listed.status_code == 200
        assert all("token" not in item for item in listed.json())

        headers = {"Authorization": f"Bearer {secret}"}
        async with AsyncClient(
            transport=transport,
            base_url="http://localhost",
            headers=headers,
        ) as audit_client:
            non_audit = await audit_client.get("/api/v1/system/overview")
            assert non_audit.status_code == 401

            media = await audit_client.get(f"/api/v1/assets/{uuid.uuid4()}/media/preview")
            assert media.status_code == 401

        revoked = await owner.delete(f"/api/v1/audit/tokens/{token_id}")
        assert revoked.status_code == 204

        rejected = await owner.get(
            "/api/v1/audit/inference-profiles",
            headers={"Authorization": f"Bearer {secret}"},
        )
        assert rejected.status_code == 401


async def test_audit_token_rejects_unknown_scope(authenticated_context) -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport,
        base_url="http://localhost",
        cookies={"star_session": authenticated_context["token"]},
    ) as client:
        response = await client.post(
            "/api/v1/audit/tokens",
            json={"name": "bad", "scopes": ["media:read"]},
        )
        assert response.status_code == 422
