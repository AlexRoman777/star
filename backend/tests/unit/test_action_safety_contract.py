from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Iterator
from typing import Any

from fastapi import APIRouter

from star.api.router import api_router
from star.services.action_safety import (
    ACTION_SAFETY_BY_ENDPOINT,
    ACTION_SAFETY_PROFILES,
    READ_ONLY_GET_COMMIT_ENDPOINTS,
    STATE_CHANGING_GET_ENDPOINTS,
    ActionSafetyClass,
    validate_action_safety_contract,
)

MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _routes(router: APIRouter) -> Iterator[Any]:
    for route in router.routes:
        child = getattr(route, "original_router", None)
        if child is not None:
            yield from _routes(child)
        else:
            yield route


def test_every_mutating_api_action_has_an_explicit_safety_contract() -> None:
    mutating_endpoints = {
        route.name
        for route in _routes(api_router)
        if set(getattr(route, "methods", set()) or set()) & MUTATING_METHODS
    }
    get_endpoints = {
        route.name
        for route in _routes(api_router)
        if "GET" in set(getattr(route, "methods", set()) or set())
    }

    assert get_endpoints >= STATE_CHANGING_GET_ENDPOINTS
    validate_action_safety_contract(mutating_endpoints | set(STATE_CHANGING_GET_ENDPOINTS))


def test_get_handlers_cannot_add_an_unreviewed_commit_side_effect() -> None:
    direct_commit_endpoints: set[str] = set()
    get_endpoints: set[str] = set()
    for route in _routes(api_router):
        if "GET" not in set(getattr(route, "methods", set()) or set()):
            continue
        get_endpoints.add(route.name)
        source = textwrap.dedent(inspect.getsource(route.endpoint))
        tree = ast.parse(source)
        if any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "commit"
            for node in ast.walk(tree)
        ):
            direct_commit_endpoints.add(route.name)

    reviewed = STATE_CHANGING_GET_ENDPOINTS | READ_ONLY_GET_COMMIT_ENDPOINTS
    assert get_endpoints >= reviewed
    assert direct_commit_endpoints <= reviewed


def test_action_safety_profiles_never_authorize_media_deletion() -> None:
    assert set(ACTION_SAFETY_PROFILES) == set(ActionSafetyClass)
    assert not any(profile.immich_media_delete for profile in ACTION_SAFETY_PROFILES.values())
    assert {
        safety_class
        for safety_class, profile in ACTION_SAFETY_PROFILES.items()
        if profile.immich_metadata_write
    } == {ActionSafetyClass.IMMICH_WRITEBACK}
    assert (
        ACTION_SAFETY_PROFILES[ActionSafetyClass.LOCAL_DESTRUCTIVE].external_effect
        == "never deletes or trashes Immich media"
    )
    assert ACTION_SAFETY_BY_ENDPOINT["approve_writeback"] == ActionSafetyClass.IMMICH_WRITEBACK
    assert ACTION_SAFETY_BY_ENDPOINT["execute_library_reset"] == ActionSafetyClass.LOCAL_DESTRUCTIVE


def test_finite_file_and_media_responses_close_database_before_body_streaming() -> None:
    required = {
        "get_asset_preview",
        "get_asset_thumbnail",
        "get_group_task_preview",
        "get_task_preview",
        "stream_system_events",
    }
    observed: set[str] = set()
    for route in _routes(api_router):
        if route.name not in required:
            continue
        session_dependencies = [
            dependency
            for dependency in route.dependant.dependencies
            if getattr(dependency.call, "__name__", None) == "get_session"
        ]
        assert len(session_dependencies) == 1
        assert session_dependencies[0].scope == "function"
        observed.add(route.name)
    assert observed == required
