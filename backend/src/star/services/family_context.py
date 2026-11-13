from __future__ import annotations

import json
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import get_settings
from star.core.crypto import SecretCipher
from star.db.models import AssetFaceReference, FamilyRosterMember, PersonReference

_ROSTER_CACHE_TTL_SECONDS = 30.0
_roster_cache: tuple[float, tuple[FamilyProfile, ...]] | None = None


@dataclass(frozen=True)
class FamilyProfile:
    """One decrypted, owner-registered family member or pet."""

    name: str
    role: str  # parent | child | pet
    kind: str  # person | pet
    aliases: tuple[str, ...]
    species: str | None = None
    breed: str | None = None
    pronouns: str | None = None


# The roster is the owner's own data. It starts empty and is filled in the web UI
# (Settings, Family), where names and aliases are stored encrypted.
DEFAULT_FAMILY_ROSTER: tuple[FamilyProfile, ...] = ()


def _cipher() -> SecretCipher | None:
    with suppress(RuntimeError):
        return SecretCipher(get_settings().require_encryption_key())
    return None


def normalized_alias(value: str) -> str:
    return " ".join(value.casefold().split())


def _profile_from_row(row: FamilyRosterMember, cipher: SecretCipher) -> FamilyProfile | None:
    try:
        name = cipher.decrypt(row.encrypted_canonical_name)
        raw_aliases = json.loads(cipher.decrypt(row.encrypted_aliases))
    except ValueError, json.JSONDecodeError:
        return None
    aliases = tuple(normalized_alias(str(alias)) for alias in raw_aliases if isinstance(alias, str))
    if not name or not aliases:
        return None
    return FamilyProfile(
        name=name,
        role=row.role,
        kind=row.kind,
        aliases=aliases,
        species=row.species,
        breed=row.breed,
        pronouns=row.pronouns,
    )


def invalidate_family_roster_cache() -> None:
    global _roster_cache
    _roster_cache = None


async def ensure_family_roster_seeded(
    session: AsyncSession,
    cipher: SecretCipher | None = None,
) -> None:
    """Insert the encrypted default roster when the table is empty."""

    cipher = cipher or _cipher()
    if cipher is None:
        return
    existing = await session.scalar(select(FamilyRosterMember.id).limit(1))
    if existing is not None:
        return
    for profile in DEFAULT_FAMILY_ROSTER:
        session.add(
            FamilyRosterMember(
                kind=profile.kind,
                role=profile.role,
                encrypted_canonical_name=cipher.encrypt(profile.name),
                encrypted_aliases=cipher.encrypt(json.dumps(list(profile.aliases))),
                species=profile.species,
                breed=profile.breed,
                pronouns=profile.pronouns,
            )
        )
    await session.flush()
    invalidate_family_roster_cache()


async def load_family_roster(session: AsyncSession) -> tuple[FamilyProfile, ...]:
    """Return the active roster, seeding the owner defaults on first use.

    Names and aliases live encrypted in ``family_roster_members``. When no
    encryption key is configured (bare unit-test settings), the in-memory
    defaults serve reads without persisting anything.
    """

    global _roster_cache
    cached = _roster_cache
    if cached is not None and time.monotonic() - cached[0] < _ROSTER_CACHE_TTL_SECONDS:
        return cached[1]

    cipher = _cipher()
    if cipher is None:
        return DEFAULT_FAMILY_ROSTER

    await ensure_family_roster_seeded(session)
    rows = list(await session.scalars(select(FamilyRosterMember)))
    profiles = tuple(
        profile
        for row in rows
        if row.active
        for profile in (_profile_from_row(row, cipher),)
        if profile is not None
    )
    _roster_cache = (time.monotonic(), profiles)
    return profiles


def attributed_family_member(
    display_name: str,
    roster: tuple[FamilyProfile, ...] = DEFAULT_FAMILY_ROSTER,
) -> dict[str, str] | None:
    """Map an exact Immich-attributed name to the owner's roster profile."""

    normalized = normalized_alias(display_name)
    for profile in roster:
        if normalized in profile.aliases:
            member: dict[str, str] = {"name": profile.name, "role": profile.role}
            if profile.species:
                member["species"] = profile.species
            if profile.breed:
                member["breed"] = profile.breed
            if profile.pronouns:
                member["pronouns"] = profile.pronouns
            return member
    return None


async def family_source_data(
    session: AsyncSession,
    source_data: dict[str, Any],
    display_name: str,
) -> dict[str, Any]:
    """Persist a bounded local marker without copying Immich's clear-text person name."""

    result = dict(source_data)
    member = attributed_family_member(display_name, await load_family_roster(session))
    if member is None:
        result.pop("star_family_member", None)
    else:
        result["star_family_member"] = member
    return result


def _marker_member(raw_member: object) -> dict[str, str] | None:
    if not isinstance(raw_member, dict):
        return None
    if not raw_member.get("name") or raw_member.get("role") not in {"parent", "child", "pet"}:
        return None
    return {
        key: str(value)
        for key, value in raw_member.items()
        if key in {"name", "role", "species", "breed", "pronouns"} and value
    }


async def trusted_family_contexts(
    session: AsyncSession,
    asset_ids: list[uuid.UUID],
) -> dict[uuid.UUID, dict[str, object]]:
    """Batch variant of :func:`trusted_family_context` for group enqueues.

    One face/person join covers every candidate so a four-photo group costs one
    query round instead of four.
    """

    contexts: dict[uuid.UUID, dict[str, object]] = {asset_id: {} for asset_id in asset_ids}
    if not asset_ids:
        return contexts
    rows = list(
        (
            await session.execute(
                select(AssetFaceReference.asset_id, PersonReference)
                .join(
                    PersonReference,
                    (AssetFaceReference.connection_id == PersonReference.connection_id)
                    & (AssetFaceReference.immich_person_id == PersonReference.immich_person_id),
                )
                .where(
                    AssetFaceReference.asset_id.in_(asset_ids),
                    AssetFaceReference.access_state == "available",
                    AssetFaceReference.is_visible.is_(True),
                    PersonReference.access_state == "available",
                    PersonReference.is_hidden.is_(False),
                )
            )
        ).all()
    )
    if not rows:
        return contexts
    roster = await load_family_roster(session)
    cipher = _cipher()
    persons_by_asset: dict[uuid.UUID, list[PersonReference]] = {}
    for asset_id, person in rows:
        if asset_id is not None:
            persons_by_asset.setdefault(asset_id, []).append(person)
    for asset_id, persons in persons_by_asset.items():
        contexts[asset_id] = _context_from_persons(persons, roster=roster, cipher=cipher)
    return contexts


def _context_from_persons(
    persons: list[PersonReference],
    *,
    roster: tuple[FamilyProfile, ...],
    cipher: SecretCipher | None,
) -> dict[str, object]:
    members: dict[str, dict[str, str]] = {}
    for person in persons:
        member = _marker_member(person.source_data.get("star_family_member"))
        if member is None and cipher is not None:
            try:
                member = attributed_family_member(
                    cipher.decrypt(person.encrypted_display_name), roster
                )
            except ValueError:
                member = None
        if member is not None:
            members[member["name"]] = member
    people = sorted(
        (member for member in members.values() if member["role"] in {"parent", "child"}),
        key=lambda item: (item["role"], item["name"]),
    )
    pets = sorted(
        (member for member in members.values() if member["role"] == "pet"),
        key=lambda item: item["name"],
    )
    if not people and not pets:
        return {}
    roster_person_count = sum(profile.kind == "person" for profile in roster)
    context: dict[str, object] = {
        "source": "immich_attributed_people",
        "members": people,
        "member_count": len(people),
        "parent_count": sum(member["role"] == "parent" for member in people),
        "child_count": sum(member["role"] == "child" for member in people),
        "multiple_core_family_members": len(people) >= 2,
        "whole_core_family": bool(people) and len(people) == roster_person_count,
        "identity_is_attributed_not_visually_inferred": True,
    }
    if pets:
        context["pets"] = pets
        context["pet_count"] = len(pets)
        context["pets_attributed_not_visually_inferred"] = True
    return context


async def trusted_family_context(
    session: AsyncSession,
    asset_id: uuid.UUID,
) -> dict[str, object]:
    """Return trusted Immich-attributed family facts, never inferred identities."""

    rows = list(
        await session.scalars(
            select(PersonReference)
            .join(
                AssetFaceReference,
                (AssetFaceReference.connection_id == PersonReference.connection_id)
                & (AssetFaceReference.immich_person_id == PersonReference.immich_person_id),
            )
            .where(
                AssetFaceReference.asset_id == asset_id,
                AssetFaceReference.access_state == "available",
                AssetFaceReference.is_visible.is_(True),
                PersonReference.access_state == "available",
                PersonReference.is_hidden.is_(False),
            )
        )
    )
    if not rows:
        return {}

    roster = await load_family_roster(session)
    return _context_from_persons(rows, roster=roster, cipher=_cipher())
