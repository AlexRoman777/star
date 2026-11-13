from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings
from star.core.crypto import SecretCipher
from star.db.base import utc_now
from star.db.models import (
    Asset,
    AssetFaceReference,
    AssetImmichContext,
    ImmichConnection,
    ImmichInstance,
    PersonReference,
)
from star.domain.enums import CredentialKind
from star.immich.client import ImmichApiError, ImmichClient
from star.services.connections import get_active_credential
from star.services.family_context import family_source_data


def _bounds(face: dict[str, Any]) -> dict[str, int]:
    return {
        key: int(face.get(key, 0) or 0)
        for key in ("boundingBoxX1", "boundingBoxY1", "boundingBoxX2", "boundingBoxY2")
    }


def _asset_response_faces(asset_info: dict[str, Any]) -> list[dict[str, Any]]:
    faces: list[dict[str, Any]] = []
    people = asset_info.get("people")
    if isinstance(people, list):
        for person_value in people:
            if not isinstance(person_value, dict):
                continue
            person_faces = person_value.get("faces")
            if not isinstance(person_faces, list):
                continue
            for face_value in person_faces:
                if isinstance(face_value, dict):
                    faces.append({**face_value, "person": person_value})
    unassigned = asset_info.get("unassignedFaces")
    if isinstance(unassigned, list):
        faces.extend({**face, "person": None} for face in unassigned if isinstance(face, dict))
    return faces


async def _upsert_people_and_faces(
    session: AsyncSession,
    *,
    asset: Asset,
    connection: ImmichConnection,
    faces: list[dict[str, Any]],
    cipher: SecretCipher,
) -> None:
    for face_value in faces:
        person_value = face_value.get("person")
        person = person_value if isinstance(person_value, dict) else None
        person_id = str(person.get("id") or "") if person else ""
        if person_id and person is not None:
            display_name = str(person.get("name") or "Unnamed person")
            person_row = await session.scalar(
                select(PersonReference).where(
                    PersonReference.connection_id == connection.id,
                    PersonReference.immich_person_id == person_id,
                )
            )
            person_values = {
                "immich_owner_user_id": connection.immich_user_id,
                "encrypted_display_name": cipher.encrypt(display_name),
                "is_favorite": bool(person.get("isFavorite", False)),
                "is_hidden": bool(person.get("isHidden", False)),
                "access_state": "available",
                "source_data": await family_source_data(
                    session,
                    {key: value for key, value in person.items() if key != "name"},
                    display_name,
                ),
            }
            if person_row is None:
                session.add(
                    PersonReference(
                        connection_id=connection.id,
                        immich_person_id=person_id,
                        **person_values,
                    )
                )
            else:
                for key, value in person_values.items():
                    setattr(person_row, key, value)

        face_id = str(face_value.get("id") or "")
        if not face_id:
            continue
        face_row = await session.scalar(
            select(AssetFaceReference).where(
                AssetFaceReference.connection_id == connection.id,
                AssetFaceReference.immich_face_id == face_id,
            )
        )
        face_values = {
            "asset_id": asset.id,
            "immich_person_id": person_id or None,
            "is_visible": not bool(person and person.get("isHidden", False)),
            "access_state": "available",
            "bounding_box": _bounds(face_value),
            "source_data": face_value,
        }
        if face_row is None:
            session.add(
                AssetFaceReference(
                    connection_id=connection.id,
                    immich_face_id=face_id,
                    **face_values,
                )
            )
        else:
            for key, value in face_values.items():
                setattr(face_row, key, value)


async def refresh_immich_asset_context(
    session: AsyncSession,
    *,
    asset: Asset,
    settings: Settings,
) -> AssetImmichContext:
    expected_revision_id = asset.current_revision_id
    asset_id = asset.id
    connection_id = asset.connection_id
    immich_asset_id = asset.immich_asset_id
    connection = await session.get(ImmichConnection, asset.connection_id)
    if connection is None:
        raise ValueError("asset connection is unavailable")
    instance = await session.get(ImmichInstance, connection.instance_id)
    credential = await get_active_credential(
        session,
        connection_id=connection.id,
        kind=CredentialKind.READ,
    )
    if instance is None or credential is None:
        raise ValueError("asset connection is missing an active read credential")

    cipher = SecretCipher(settings.require_encryption_key())
    api_key = cipher.decrypt(credential.encrypted_secret)
    instance_id = instance.id
    base_url = instance.base_url
    permission_snapshot: list[str] = list(connection.permission_snapshot)
    # Reading ORM rows starts a transaction. Never retain it while Immich is
    # answering several HTTP requests; reset and worker publication must not
    # wait behind an owner's network call.
    await session.commit()
    errors: dict[str, str] = {}
    custom_metadata: list[dict[str, Any]] = []
    ocr: list[dict[str, Any]] = []
    direct_faces: list[dict[str, Any]] = []
    permissions = set(permission_snapshot)

    async with ImmichClient(base_url, api_key) as client:
        asset_info = await client.get_asset(immich_asset_id)
        optional_requests: list[tuple[str, Awaitable[list[dict[str, Any]]]]] = [
            ("custom_metadata", client.get_asset_metadata(immich_asset_id)),
            ("ocr", client.get_asset_ocr(immich_asset_id)),
        ]
        if "face.read" in permissions:
            optional_requests.append(("faces", client.get_asset_faces(immich_asset_id)))
        else:
            errors["faces"] = (
                "The current read key lacks face.read. Faces embedded in asset.read are still "
                "shown; add face.read for the complete face endpoint."
            )
        results = await asyncio.gather(
            *(request for _, request in optional_requests),
            return_exceptions=True,
        )
        for (label, _), result in zip(optional_requests, results, strict=True):
            if isinstance(result, BaseException):
                if isinstance(result, ImmichApiError) and result.status_code == 404:
                    continue
                status = result.status_code if isinstance(result, ImmichApiError) else None
                errors[label] = (
                    f"Immich returned {status} for this optional section."
                    if status
                    else "The optional Immich section could not be retrieved."
                )
            elif label == "custom_metadata":
                custom_metadata = result
            elif label == "ocr":
                ocr = result
            elif label == "faces":
                direct_faces = result

    faces = direct_faces or _asset_response_faces(asset_info)
    locked_asset = await session.scalar(select(Asset).where(Asset.id == asset_id).with_for_update())
    locked_connection = await session.scalar(
        select(ImmichConnection).where(ImmichConnection.id == connection_id).with_for_update()
    )
    locked_instance = (
        await session.scalar(
            select(ImmichInstance)
            .where(ImmichInstance.id == locked_connection.instance_id)
            .with_for_update()
        )
        if locked_connection
        else None
    )
    if (
        locked_asset is None
        or locked_connection is None
        or locked_instance is None
        or locked_connection.instance_id != instance_id
        or locked_instance.base_url != base_url
        or locked_connection.permission_snapshot != permission_snapshot
        or locked_asset.access_state != "available"
        or locked_asset.current_revision_id != expected_revision_id
        or locked_asset.immich_asset_id != immich_asset_id
    ):
        raise ValueError("photo or Immich connection changed during source refresh")
    await _upsert_people_and_faces(
        session,
        asset=locked_asset,
        connection=locked_connection,
        faces=faces,
        cipher=cipher,
    )
    context = await session.get(AssetImmichContext, locked_asset.id)
    values = {
        "asset_info": asset_info,
        "custom_metadata": custom_metadata,
        "ocr": ocr,
        "faces": faces,
        "fetch_errors": errors,
        "source_server_version": locked_instance.server_version,
        "fetched_at": utc_now(),
    }
    if context is None:
        context = AssetImmichContext(asset_id=locked_asset.id, **values)
        session.add(context)
    else:
        for key, value in values.items():
            setattr(context, key, value)
    await session.flush()
    return context
