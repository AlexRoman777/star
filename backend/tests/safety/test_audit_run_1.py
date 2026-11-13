"""Regression tests for the findings of the first security audit (November 2026).

Each test names the finding it pins. Where a fix is a missing check in a long request
handler, the test pins the check in source, in the same way test_invariants.py pins the
deployment files; the behaviour tests cover the parts that can run without a worker.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from star.core.config import Settings, get_settings
from star.db.base import utc_now
from star.db.models import LibraryReviewNote, User
from star.db.session import get_session
from star.main import app
from star.services.auth import bootstrap_password_setup, password_login
from star.services.library_review import record_review_result

ROOT = Path(__file__).resolve().parents[3]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


# --- Finding 1: DNS rebinding -------------------------------------------------------------


def test_nginx_forwards_the_browser_host_so_the_api_can_refuse_foreign_names() -> None:
    nginx = _source("frontend/nginx.conf")
    assert "proxy_set_header Host $http_host;" in nginx
    assert "proxy_set_header Host api;" not in nginx


async def test_the_api_refuses_a_host_name_it_was_not_told_about() -> None:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://attacker.example") as client:
        response = await client.get("/api/v1/auth/status")
    assert response.status_code == 400


# --- Root cause of findings 1 to 6: enrolment without approval ----------------------------


def test_a_new_device_starts_paused_by_default() -> None:
    assert Settings.model_fields["worker_auto_enroll_start_paused"].default is True
    enrollment = _source("backend/src/star/api/routers/workers/enrollment.py")
    assert (
        'desired_state="paused" if settings.worker_auto_enroll_start_paused else "active"'
        in enrollment
    )
    compose = _source("Deployment/coordinator/compose.yaml")
    expected = "STAR_WORKER_AUTO_ENROLL_START_PAUSED: ${STAR_WORKER_AUTO_ENROLL_START_PAUSED:-true}"
    assert expected in compose


# --- Finding 4: a failure report is bound to the task's own photographs --------------------


def test_highlight_failure_only_counts_for_a_photograph_in_the_task() -> None:
    completion = _source("backend/src/star/api/routers/workers/completion.py")
    guard = completion.index('task.task_type == "group.highlight_compare"')
    call = completion.index("await mark_highlight_source_unavailable(", guard)
    assert "payload.source_asset_id in task_source_asset_ids(task)" in completion[guard:call]


# --- Finding 5: a library review result is bound to its task and note ----------------------


def test_library_review_result_is_bound_to_its_task_and_note() -> None:
    completion = _source("backend/src/star/api/routers/workers/completion.py")
    start = completion.index("async def complete_library_review_task(")
    store = completion.index("record_review_result(", start)
    handler = completion[start:store]
    assert 'task.task_type != "library.review"' in handler
    assert 'task.target_ref.get("note_id")' in handler
    assert 'note.state == "complete"' in handler


def test_review_result_keeps_only_objects() -> None:
    note = LibraryReviewNote(state="queued", summary="", findings=[], proposals=[])
    record_review_result(
        note,
        {
            "summary": "A reading",
            "findings": [{"title": "kept"}, "dropped", 7, None],
            "proposals": "not a list",
        },
        model_key="test-model",
    )
    assert note.findings == [{"title": "kept"}]
    assert note.proposals == []
    assert note.state == "complete"


# --- Finding 7: the highlight overview needs a login ---------------------------------------


async def test_highlight_overview_requires_a_session(session_factory, test_settings) -> None:
    async def override_session() -> AsyncIterator[AsyncSession]:
        async with session_factory() as database_session:
            yield database_session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: test_settings
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://localhost") as client:
            response = await client.get("/api/v1/highlights/overview")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


# --- Finding 8: an expired lockout clears the counter --------------------------------------


async def test_one_wrong_password_after_an_expired_lock_does_not_lock_again(
    session_factory,
    test_settings,
) -> None:
    async with session_factory() as session:
        await bootstrap_password_setup(
            session,
            test_settings,
            username="owner",
            display_name="Owner",
            password="correct-horse-1",
        )
        await session.commit()

    for _ in range(5):
        async with session_factory() as session:
            with pytest.raises(ValueError):
                await password_login(
                    session, test_settings, username="owner", password="wrong-password"
                )
            await session.commit()

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.username == "owner"))
        assert user is not None and user.password_locked_until is not None
        user.password_locked_until = utc_now() - timedelta(minutes=1)
        await session.commit()

    async with session_factory() as session:
        with pytest.raises(ValueError):
            await password_login(
                session, test_settings, username="owner", password="wrong-password"
            )
        await session.commit()

    async with session_factory() as session:
        user = await session.scalar(select(User).where(User.username == "owner"))
        assert user is not None
        assert user.failed_password_attempts == 1
        assert user.password_locked_until is None
        result = await password_login(
            session, test_settings, username="owner", password="correct-horse-1"
        )
        assert result is not None


# --- Finding 9: first setup is serialised on PostgreSQL ------------------------------------


def test_first_setup_takes_an_advisory_lock_on_postgresql() -> None:
    auth = _source("backend/src/star/services/auth.py")
    start = auth.index("async def bootstrap_password_setup(")
    count = auth.index("users_exist = await has_users(session)", start)
    assert "pg_advisory_xact_lock" in auth[start:count]
