"""A photograph's review must survive Immich touching its metadata.

Ten thousand re-reviews produced two hundred published results and thirty
thousand cancelled tasks while both machines ran flat out. Every sync minted a
new asset revision, and a new revision discarded every review in flight for
that photograph.
"""

from __future__ import annotations

from star.services.sync import VOLATILE_METADATA_KEYS, _metadata_digest


def test_immich_bookkeeping_does_not_mint_a_new_revision() -> None:
    """`updatedAt` moves for reasons that have nothing to do with the picture.

    Immich's own machine-learning jobs, a regenerated thumbnail and Star's
    approved write-back all bump it. If it decides revision identity, a
    photograph gets a new revision on nearly every sync.
    """

    photo = {
        "id": "asset-1",
        "checksum": "abc",
        "width": 4032,
        "height": 3024,
        "fileSizeInByte": 5_242_880,
        "isFavorite": False,
        "rating": 3,
        "updatedAt": "2026-09-09T09:00:00Z",
        "thumbhash": "aaaa",
    }
    touched = {**photo, "updatedAt": "2026-09-09T17:41:00Z", "thumbhash": "bbbb"}
    assert _metadata_digest(photo) == _metadata_digest(touched)

    # What the owner actually changed still makes a new revision.
    rated = {**photo, "rating": 5}
    favourited = {**photo, "isFavorite": True}
    replaced = {**photo, "checksum": "def"}
    assert _metadata_digest(rated) != _metadata_digest(photo)
    assert _metadata_digest(favourited) != _metadata_digest(photo)
    assert _metadata_digest(replaced) != _metadata_digest(photo)


def test_the_volatile_list_stays_bookkeeping_only() -> None:
    """Nothing that changes what a review concludes may be ignored here."""

    assert {"updatedAt", "thumbhash"} == VOLATILE_METADATA_KEYS


async def test_a_rating_change_no_longer_throws_away_a_finished_review(session) -> None:
    """A review is about the photograph, not about its metadata.

    When the owner rates or favourites a picture in Immich, Star records a new
    revision. Treating that as a different photograph discarded the completed
    review and every task behind it.
    """

    from star.db.base import utc_now
    from star.db.models import (
        AnalysisGeneration,
        Asset,
        AssetRevision,
        ImmichConnection,
        ImmichInstance,
    )
    from star.domain.enums import AccountPurpose, HealthStatus, MediaType
    from star.services.analysis_generations import (
        revision_still_describes_the_same_photograph,
    )

    instance = ImmichInstance(
        stable_instance_id="revision-churn",
        base_url="https://immich.home.arpa",
        health=HealthStatus.HEALTHY,
    )
    session.add(instance)
    await session.flush()
    connection = ImmichConnection(
        instance_id=instance.id,
        immich_user_id="owner",
        display_label="Archive",
        purpose=AccountPurpose.OTHER,
        processing_profile="family",
        model_profile="family",
        health=HealthStatus.HEALTHY,
    )
    session.add(connection)
    await session.flush()
    asset = Asset(
        instance_id=instance.id,
        connection_id=connection.id,
        immich_asset_id="churned",
        immich_owner_user_id="owner",
        media_type=MediaType.IMAGE,
    )
    session.add(asset)
    await session.flush()

    def _revision(digest: str, **overrides: object) -> AssetRevision:
        fields: dict[str, object] = {
            "asset_id": asset.id,
            "immich_updated_at": utc_now(),
            "metadata_digest": digest,
            "normalized_metadata": {},
            "file_size": 5_242_880,
            "width": 4032,
            "height": 3024,
        }
        fields.update(overrides)
        return AssetRevision(**fields)

    reviewed = _revision("a" * 64)
    session.add(reviewed)
    await session.flush()
    asset.current_revision_id = reviewed.id
    generation = AnalysisGeneration(
        asset_id=asset.id,
        asset_revision_id=reviewed.id,
        sequence=1,
        state="building",
        start_stage="source",
        expanded_stages=["primary_review"],
        reuse_policy="none",
        publication_policy="atomic",
        model_policy_version="test",
        prompt_version="test",
        schema_version="test",
        calibration_version="test",
        reason="continuous_contract_refresh",
        scope={},
    )
    session.add(generation)
    await session.flush()

    # The owner rates it in Immich: same picture, new revision. The review stands.
    rated = _revision("b" * 64)
    session.add(rated)
    await session.flush()
    asset.current_revision_id = rated.id
    assert (
        await revision_still_describes_the_same_photograph(
            session, generation=generation, asset=asset
        )
        is True
    )

    # A different picture, or one on its way out of the library, does not.
    replaced = _revision("c" * 64, width=1920, height=1080, file_size=1_000_000)
    session.add(replaced)
    await session.flush()
    asset.current_revision_id = replaced.id
    assert (
        await revision_still_describes_the_same_photograph(
            session, generation=generation, asset=asset
        )
        is False
    )

    trashed = _revision("d" * 64, is_trashed=True)
    session.add(trashed)
    await session.flush()
    asset.current_revision_id = trashed.id
    assert (
        await revision_still_describes_the_same_photograph(
            session, generation=generation, asset=asset
        )
        is False
    )

    # Unknown dimensions mean nothing can be confirmed, so the strict answer stands.
    unknown = _revision("e" * 64, width=None, height=None)
    session.add(unknown)
    await session.flush()
    asset.current_revision_id = unknown.id
    assert (
        await revision_still_describes_the_same_photograph(
            session, generation=generation, asset=asset
        )
        is False
    )
