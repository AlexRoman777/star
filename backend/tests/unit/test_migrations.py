from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[3]
VERSIONS = ROOT / "backend" / "alembic" / "versions"


def _script_directory() -> ScriptDirectory:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "backend" / "alembic"))
    return ScriptDirectory.from_config(config)


def test_migration_history_is_the_squashed_baseline_plus_data_steps() -> None:
    script = _script_directory()
    assert script.get_heads() == ["20260920_0017"]
    revisions = [item.revision for item in script.walk_revisions()]
    assert revisions == [
        "20260920_0017",
        "20260919_0016",
        "20260916_0015",
        "20260916_0014",
        "20260916_0013",
        "20260916_0012",
        "20260915_0011",
        "20260909_0010",
        "20260906_0009",
        "20260906_0008",
        "20260906_0007",
        "20260906_0006",
        "20260906_0005",
        "20260905_0004",
        "20260904_0003",
        "20260904_0002",
        "20260904_0001",
    ]
    assert script.get_revision("20260904_0001").down_revision is None


def test_baseline_applies_the_captured_v180_schema_verbatim() -> None:
    sql = (VERSIONS / "baseline_v180_schema.sql").read_text()
    source = (VERSIONS / "20260904_0001_baseline.py").read_text()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in sql
    assert "CREATE TABLE public.assets" in sql
    assert "CREATE TABLE public.family_roster_members" not in sql
    assert "alembic_version" not in sql
    assert 'LEGACY_HEAD = "20260903_0105"' in source
    assert "split_statements" in source
    # The baseline can only be created, never reversed; backups are the way back.
    assert "cannot be downgraded" in source


def test_env_restamps_released_v180_and_only_known_candidate_heads() -> None:
    env = (ROOT / "backend" / "alembic" / "env.py").read_text()
    assert 'BASELINE_REVISION = "20260904_0001"' in env
    assert '"20260903_0105"' in env
    assert '"20260903_0108"' in env
    assert "predates the v180 baseline" in env
    assert "create_all" not in env


def test_bridge_and_data_migration_are_complete_and_non_destructive() -> None:
    source = (VERSIONS / "20260904_0002_star_rating_senior_review.py").read_text()
    assert "asset.fast_rating" in source
    assert "asset.apple_order_check" in source
    assert "asset.deep_review_experiment" in source
    assert "family_roster_members" in source
    assert "asset.photo_dossier" in source
    assert "state IN " in source
    assert "('PLANNED', 'QUEUED', 'RETRY_WAITING')" in source
    assert "DELETE" not in source.upper().replace("DELETE FROM", "DELETE")  # data-only, no deletes


def test_migration_identifiers_fit_postgresql_limit() -> None:
    for path in VERSIONS.glob("*.py"):
        assert len(path.stem.split("_")[0] + "_" + path.stem.split("_")[1]) <= 32


def test_quick_rating_removal_is_a_bounded_non_destructive_data_step() -> None:
    source = (VERSIONS / "20260906_0007_remove_quick_rating.py").read_text()
    assert 'down_revision = "20260906_0006"' in source
    assert "quick_rating" in source
    assert "group.cohort_triage" in source
    assert "asset.clip_embedding" in source
    assert "asset.description_embedding" in source
    assert "cohort_triage_state" in source
    assert "TaskTypeRetired" in source
    assert "'QUEUED', 'RETRY_WAITING', 'RUNNING', 'CANCEL_REQUESTED'" in source
    assert "DELETE" not in source.upper()
    assert "DROP" not in source.upper()
