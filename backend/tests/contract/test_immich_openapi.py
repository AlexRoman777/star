from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
VERSIONS = ("v2.7.5", "v3.0.3")


def _spec(version: str = "v3.0.3") -> dict:
    path = FIXTURE_ROOT / f"immich-{version}-openapi.json"
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("version", VERSIONS)
def test_pinned_immich_sync_contract(version: str) -> None:
    spec = _spec(version)
    stream = spec["paths"]["/sync/stream"]["post"]
    acknowledge = spec["paths"]["/sync/ack"]["post"]
    assert stream["operationId"] == "getSyncStream"
    assert stream["x-immich-state"] == "Stable"
    assert stream["x-immich-permission"] == "sync.stream"
    assert acknowledge["operationId"] == "sendSyncAck"
    assert acknowledge["x-immich-permission"] == "syncCheckpoint.update"


def test_sync_request_types_match_each_pinned_contract() -> None:
    expectations = {
        "v2.7.5": {"AssetsV1", "AlbumsV1"},
        "v3.0.3": {"AssetsV2", "AlbumsV2"},
    }
    common = {"AssetExifsV1", "AlbumToAssetsV1", "PeopleV1", "AssetFacesV2"}
    for version, versioned in expectations.items():
        spec = _spec(version)
        supported = set(spec["components"]["schemas"]["SyncRequestType"]["enum"])
        requested = versioned | common
        assert requested <= supported


@pytest.mark.parametrize("version", VERSIONS)
def test_random_sampling_and_library_overview_contracts(version: str) -> None:
    spec = _spec(version)
    random_search = spec["paths"]["/search/random"]["post"]
    random_size = spec["components"]["schemas"]["RandomSearchDto"]["properties"]["size"]
    assert random_search["x-immich-state"] == "Stable"
    assert random_search["x-immich-permission"] == "asset.read"
    assert random_size["minimum"] == 1
    assert random_size["maximum"] == 1000
    assert spec["paths"]["/assets/statistics"]["get"]["x-immich-permission"] == ("asset.statistics")
    assert spec["paths"]["/people"]["get"]["x-immich-permission"] == "person.read"
    assert spec["paths"]["/albums"]["get"]["x-immich-permission"] == "album.read"


def test_pinned_immich_rating_contract_and_zero_semantics() -> None:
    spec = _spec()
    operation = spec["paths"]["/assets"]["put"]
    rating = spec["components"]["schemas"]["AssetBulkUpdateDto"]["properties"]["rating"]
    assert operation["x-immich-permission"] == "asset.update"
    assert rating["minimum"] == -1
    assert rating["maximum"] == 5
    assert rating["nullable"] is True
    history = rating["x-immich-history"]
    assert any(
        "0 as a rating is no longer valid" in item.get("description", "") for item in history
    )


@pytest.mark.parametrize("version", VERSIONS)
def test_pinned_immich_thumbnail_and_tag_contracts(version: str) -> None:
    spec = _spec(version)
    thumbnail = spec["paths"]["/assets/{id}/thumbnail"]["get"]
    tags = spec["paths"]["/tags"]["get"]
    tag_assets = spec["paths"]["/tags/assets"]["put"]
    assert thumbnail["x-immich-state"] == "Stable"
    assert thumbnail["x-immich-permission"] == "asset.view"
    size_parameter = next(item for item in thumbnail["parameters"] if item["name"] == "size")
    size_schema = size_parameter["schema"]
    enum_schema = spec["components"]["schemas"][size_schema["$ref"].rsplit("/", 1)[-1]]
    assert "fullsize" in enum_schema["enum"]
    assert "redirect to downloadAsset" in thumbnail["description"]
    assert tags["x-immich-permission"] == "tag.read"
    assert tag_assets["x-immich-state"] == "Stable"
    assert tag_assets["x-immich-permission"] == "tag.asset"


@pytest.mark.parametrize("version", VERSIONS)
def test_pinned_least_privilege_read_context_contract(version: str) -> None:
    spec = _spec(version)
    assert spec["paths"]["/users/me"]["get"]["x-immich-permission"] == "user.read"
    assert spec["paths"]["/assets/{id}"]["get"]["x-immich-permission"] == "asset.read"
    assert spec["paths"]["/albums"]["get"]["x-immich-permission"] == "album.read"
    assert spec["paths"]["/people"]["get"]["x-immich-permission"] == "person.read"
    assert spec["paths"]["/people/{id}/thumbnail"]["get"]["x-immich-permission"] == "person.read"
    assert spec["paths"]["/duplicates"]["get"]["x-immich-permission"] == "duplicate.read"
    assert spec["paths"]["/assets/{id}/metadata"]["get"]["x-immich-permission"] == ("asset.read")
    assert spec["paths"]["/assets/{id}/ocr"]["get"]["x-immich-permission"] == ("asset.read")
    assert spec["paths"]["/faces"]["get"]["x-immich-permission"] == "face.read"
