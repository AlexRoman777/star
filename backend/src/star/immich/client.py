from __future__ import annotations

import json
import math
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx

MAX_IMAGE_RESPONSE_BYTES = 50 * 1024 * 1024


class ImmichApiError(RuntimeError):
    def __init__(
        self, message: str, *, status_code: int | None = None, retry_after: str | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after_seconds = 0.0
        if retry_after:
            try:
                self.retry_after_seconds = max(0.0, float(retry_after))
            except ValueError:
                with suppress(ValueError, TypeError, OverflowError):
                    self.retry_after_seconds = max(
                        0.0,
                        (parsedate_to_datetime(retry_after) - datetime.now(UTC)).total_seconds(),
                    )
            if not math.isfinite(self.retry_after_seconds):
                self.retry_after_seconds = 0.0
            self.retry_after_seconds = min(self.retry_after_seconds, 86400 * 7)


@dataclass(frozen=True)
class ImmichIdentity:
    user_id: str
    user_name: str
    is_admin: bool
    api_key_id: str
    api_key_name: str
    permissions: list[str]
    server_version: str


class ImmichClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        normalized = base_url.rstrip("/")
        if not normalized.endswith("/api"):
            normalized = f"{normalized}/api"
        self._client = httpx.AsyncClient(
            base_url=normalized,
            headers={"x-api-key": api_key, "accept": "application/json"},
            timeout=httpx.Timeout(timeout, read=120.0),
            transport=transport,
            follow_redirects=False,
        )

    async def __aenter__(self) -> ImmichClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_json(self, path: str) -> dict[str, Any]:
        response = await self._client.get(path)
        if response.is_error:
            raise ImmichApiError(
                f"Immich request failed with status {response.status_code}",
                status_code=response.status_code,
                retry_after=response.headers.get("retry-after"),
            )
        value = response.json()
        if not isinstance(value, dict):
            raise ImmichApiError("Immich returned an unexpected response")
        return value

    async def _get_json_list(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        response = await self._client.get(path, params=params)
        if response.is_error:
            raise ImmichApiError(
                f"Immich request failed with status {response.status_code}",
                status_code=response.status_code,
            )
        value = response.json()
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ImmichApiError("Immich returned an unexpected list response")
        return value

    async def inspect_identity(self) -> ImmichIdentity:
        user = await self._get_json("/users/me")
        key = await self._get_json("/api-keys/me")
        version = await self._get_json("/server/version")
        permissions = key.get("permissions", [])
        return ImmichIdentity(
            user_id=str(user["id"]),
            user_name=str(user.get("name") or user.get("email") or "Immich user"),
            is_admin=bool(user.get("isAdmin", False)),
            api_key_id=str(key["id"]),
            api_key_name=str(key.get("name") or "API key"),
            permissions=[str(item) for item in permissions]
            if isinstance(permissions, list)
            else [],
            server_version=".".join(
                str(version.get(part, 0)) for part in ("major", "minor", "patch")
            ),
        )

    async def iter_sync_events(
        self,
        *,
        types: list[str],
        reset: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        async with self._client.stream(
            "POST",
            "/sync/stream",
            json={"types": types, "reset": reset},
            headers={"accept": "application/jsonl"},
        ) as response:
            if response.is_error:
                await response.aread()
                raise ImmichApiError(
                    f"Immich sync failed with status {response.status_code}",
                    status_code=response.status_code,
                )
            async for line in response.aiter_lines():
                if not line.strip():
                    continue
                value = json.loads(line)
                if isinstance(value, dict):
                    yield value

    async def acknowledge(self, ack_ids: list[str]) -> None:
        if not ack_ids:
            return
        response = await self._client.post("/sync/ack", json={"acks": ack_ids[:1000]})
        if response.is_error:
            raise ImmichApiError(
                f"Immich sync acknowledgement failed with status {response.status_code}",
                status_code=response.status_code,
            )

    async def _get_bounded_image(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        label: str,
        max_bytes: int = MAX_IMAGE_RESPONSE_BYTES,
    ) -> httpx.Response:
        async with self._client.stream("GET", path, params=params) as response:
            if not response.is_success:
                await response.aread()
                raise ImmichApiError(
                    f"Immich {label} failed with status {response.status_code}",
                    status_code=response.status_code,
                )
            declared_length = response.headers.get("content-length")
            if declared_length is not None:
                try:
                    if int(declared_length) > max_bytes:
                        raise ImmichApiError(f"Immich {label} exceeded the safety limit")
                except ValueError as exc:
                    raise ImmichApiError(
                        f"Immich {label} returned an invalid content length"
                    ) from exc
            chunks: list[bytes] = []
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received > max_bytes:
                    raise ImmichApiError(f"Immich {label} exceeded the safety limit")
                chunks.append(chunk)
            return httpx.Response(
                status_code=response.status_code,
                headers=response.headers,
                content=b"".join(chunks),
                request=response.request,
            )

    async def get_thumbnail(
        self,
        asset_id: str,
        *,
        size: Literal["thumbnail", "preview", "fullsize"] = "preview",
        max_bytes: int = MAX_IMAGE_RESPONSE_BYTES,
    ) -> httpx.Response:
        if size not in {"thumbnail", "preview", "fullsize"}:
            raise ValueError(
                "Star permits only Immich thumbnail, preview, and direct fullsize derivatives"
            )
        return await self._get_bounded_image(
            f"/assets/{asset_id}/thumbnail",
            params={"size": size},
            label="thumbnail",
            max_bytes=max_bytes,
        )

    async def open_thumbnail_stream(
        self,
        asset_id: str,
        *,
        size: Literal["thumbnail", "preview", "fullsize"] = "preview",
        max_bytes: int = MAX_IMAGE_RESPONSE_BYTES,
    ) -> httpx.Response:
        if size not in {"thumbnail", "preview", "fullsize"}:
            raise ValueError(
                "Star permits only Immich thumbnail, preview, and direct fullsize derivatives"
            )
        request = self._client.build_request(
            "GET",
            f"/assets/{asset_id}/thumbnail",
            params={"size": size},
        )
        response = await self._client.send(request, stream=True)
        if not response.is_success:
            await response.aread()
            await response.aclose()
            raise ImmichApiError(
                f"Immich asset preview failed with status {response.status_code}",
                status_code=response.status_code,
            )
        declared_length = response.headers.get("content-length")
        if declared_length is not None:
            try:
                if int(declared_length) > max_bytes:
                    await response.aclose()
                    raise ImmichApiError("Immich asset preview exceeded the safety limit")
            except ValueError as exc:
                await response.aclose()
                raise ImmichApiError(
                    "Immich asset preview returned an invalid content length"
                ) from exc
        return response

    async def get_person_thumbnail(
        self,
        person_id: str,
        *,
        max_bytes: int = MAX_IMAGE_RESPONSE_BYTES,
    ) -> httpx.Response:
        return await self._get_bounded_image(
            f"/people/{person_id}/thumbnail",
            label="person thumbnail",
            max_bytes=max_bytes,
        )

    async def get_asset(self, asset_id: str) -> dict[str, Any]:
        return await self._get_json(f"/assets/{asset_id}")

    async def get_asset_metadata(self, asset_id: str) -> list[dict[str, Any]]:
        return await self._get_json_list(f"/assets/{asset_id}/metadata")

    async def get_asset_ocr(self, asset_id: str) -> list[dict[str, Any]]:
        return await self._get_json_list(f"/assets/{asset_id}/ocr")

    async def get_asset_faces(self, asset_id: str) -> list[dict[str, Any]]:
        return await self._get_json_list("/faces", params={"id": asset_id})

    async def search_random_photos(self, size: int) -> list[dict[str, Any]]:
        if not 1 <= size <= 1_000:
            raise ValueError("random search size must be between 1 and 1000")
        response = await self._client.post(
            "/search/random",
            json={
                "size": size,
                "type": "IMAGE",
                "withExif": True,
                "withPeople": True,
                "withStacked": True,
                "withDeleted": False,
            },
        )
        if response.is_error:
            raise ImmichApiError(
                f"Immich random search failed with status {response.status_code}",
                status_code=response.status_code,
            )
        value = response.json()
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ImmichApiError("Immich returned an unexpected random search response")
        return value

    async def search_photos(
        self,
        *,
        page: int,
        size: int,
        order: str,
    ) -> tuple[list[dict[str, Any]], int | None, int | None]:
        """Return a stable page of image metadata without downloading originals."""

        if page < 1 or not 1 <= size <= 1_000:
            raise ValueError("metadata search page and size are out of range")
        if order not in {"asc", "desc"}:
            raise ValueError("metadata search order must be asc or desc")
        response = await self._client.post(
            "/search/metadata",
            json={
                "page": page,
                "size": size,
                "order": order,
                "type": "IMAGE",
                "withExif": True,
                "withPeople": True,
                "withStacked": True,
                "withDeleted": False,
            },
        )
        if response.is_error:
            raise ImmichApiError(
                f"Immich metadata search failed with status {response.status_code}",
                status_code=response.status_code,
            )
        value = response.json()
        assets = value.get("assets") if isinstance(value, dict) else None
        if not isinstance(assets, dict):
            raise ImmichApiError("Immich returned an unexpected metadata search response")
        items = assets.get("items")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ImmichApiError("Immich returned an unexpected metadata search response")
        total_value = assets.get("total")
        total = total_value if isinstance(total_value, int) else None
        next_page_value = assets.get("nextPage")
        if next_page_value is None:
            next_page = None
        elif isinstance(next_page_value, int) and next_page_value > page:
            next_page = next_page_value
        elif (
            isinstance(next_page_value, str)
            and next_page_value.isdigit()
            and int(next_page_value) > page
        ):
            next_page = int(next_page_value)
        else:
            raise ImmichApiError("Immich returned an unexpected metadata search response")
        minimum_seen = (page - 1) * size + len(items)
        if total is not None and (
            total < minimum_seen or (next_page is not None and total <= page * size)
        ):
            total = None
        return items, total, next_page

    async def get_asset_statistics(
        self,
        *,
        is_favorite: bool | None = None,
        is_trashed: bool | None = None,
        visibility: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, bool | str] = {}
        if is_favorite is not None:
            params["isFavorite"] = is_favorite
        if is_trashed is not None:
            params["isTrashed"] = is_trashed
        if visibility is not None:
            params["visibility"] = visibility
        response = await self._client.get("/assets/statistics", params=params)
        if response.is_error:
            raise ImmichApiError(
                f"Immich asset statistics failed with status {response.status_code}",
                status_code=response.status_code,
            )
        value = response.json()
        if not isinstance(value, dict):
            raise ImmichApiError("Immich returned unexpected asset statistics")
        return value

    async def get_people_overview(self) -> dict[str, Any]:
        response = await self._client.get(
            "/people",
            params={"page": 1, "size": 1, "withHidden": True},
        )
        if response.is_error:
            raise ImmichApiError(
                f"Immich people overview failed with status {response.status_code}",
                status_code=response.status_code,
            )
        value = response.json()
        if not isinstance(value, dict):
            raise ImmichApiError("Immich returned an unexpected people overview")
        return value

    async def get_albums(self) -> list[dict[str, Any]]:
        return await self._get_json_list("/albums")

    async def update_rating(self, asset_id: str, rating: int | None) -> None:
        if rating is not None and not 1 <= rating <= 5:
            raise ValueError("rating must be null or between 1 and 5")
        response = await self._update_asset_fields(
            "/assets",
            json={"ids": [asset_id], "rating": rating},
        )
        if response.status_code != 204:
            raise ImmichApiError(
                f"Immich rating update failed with status {response.status_code}",
                status_code=response.status_code,
                retry_after=response.headers.get("retry-after"),
            )

    async def update_description(self, asset_id: str, description: str) -> None:
        if not description.strip():
            raise ValueError("description must not be blank")
        response = await self._update_asset_fields(
            "/assets",
            json={"ids": [asset_id], "description": description.strip()},
        )
        if response.status_code != 204:
            raise ImmichApiError(
                f"Immich description update failed with status {response.status_code}",
                status_code=response.status_code,
                retry_after=response.headers.get("retry-after"),
            )

    async def _update_asset_fields(self, path: str, *, json: dict[str, Any]) -> httpx.Response:
        # Immich v3 uses PATCH; v2 supports the same payload through PUT.
        # Fall back only when the method is unavailable, never after a timeout
        # or server failure with an unknown write outcome.
        response = await self._client.patch(path, json=json)
        if response.status_code in {404, 405}:
            response = await self._client.put(path, json=json)
        return response

    async def get_tags(self) -> list[dict[str, Any]]:
        return await self._get_json_list("/tags")

    async def create_tag(self, name: str) -> dict[str, Any]:
        response = await self._client.post("/tags", json={"name": name})
        if response.status_code != 201:
            raise ImmichApiError(
                f"Immich tag creation failed with status {response.status_code}",
                status_code=response.status_code,
                retry_after=response.headers.get("retry-after"),
            )
        value = response.json()
        if not isinstance(value, dict) or not isinstance(value.get("id"), str):
            raise ImmichApiError("Immich returned an invalid created tag")
        return value

    async def add_tags_to_asset(self, asset_id: str, tag_ids: list[str]) -> None:
        if not tag_ids:
            return
        response = await self._client.put(
            "/tags/assets",
            json={"assetIds": [asset_id], "tagIds": tag_ids},
        )
        if response.status_code != 200:
            raise ImmichApiError(
                f"Immich asset tagging failed with status {response.status_code}",
                status_code=response.status_code,
                retry_after=response.headers.get("retry-after"),
            )
