from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import get_args, get_type_hints
from urllib.parse import parse_qsl

import yaml

from star.domain.schemas import PhotoObservationModelOutput
from star.immich.client import ImmichClient
from star.main import app
from star.services.pipeline_contract import LEASE_TASK_CONTRACTS
from star.services.writeback import ALLOWED_WRITEBACK_OPERATIONS

ROOT = Path(__file__).parents[3]


def _coordinator_compose() -> dict[str, object]:
    path = ROOT / "Deployment/coordinator/compose.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


_compose = _coordinator_compose


def test_public_api_has_no_asset_delete_or_trash_route() -> None:
    dangerous_fragments = ("/trash", "/delete", "/permanent")
    for route in app.routes:
        path = getattr(route, "path", "")
        assert not any(fragment in path.lower() for fragment in dangerous_fragments)
        if "DELETE" in (getattr(route, "methods", set()) or set()):
            assert path.startswith("/api/v1/audit/tokens/")


def test_retired_worker_completion_surfaces_are_not_in_openapi() -> None:
    paths = app.openapi()["paths"]
    for suffix in (
        "complete-eye-state",
        "complete-portfolio-specialist",
        "complete-portrait-specialist",
        "complete-quality-specialist",
        "complete-subject-detection",
        "complete-synthesis",
        "complete-vlm",
        "complete-gemma-blind-review",
    ):
        assert not any(path.endswith(suffix) for path in paths)


def test_immich_client_and_writer_allow_only_approved_metadata_writeback() -> None:
    public_methods = {
        name
        for name in dir(ImmichClient)
        if not name.startswith("_") and callable(getattr(ImmichClient, name))
    }
    assert {"rating", "description", "tags"} == ALLOWED_WRITEBACK_OPERATIONS
    assert "update_rating" in public_methods
    assert "update_description" in public_methods
    assert {"get_tags", "create_tag", "add_tags_to_asset"} <= public_methods
    assert not public_methods.intersection(
        {"delete_asset", "empty_trash", "permanently_delete_asset", "trash_asset"}
    )


def test_coordinator_writer_uses_the_same_stored_connection_credential() -> None:
    compose = _coordinator_compose()
    services = compose["services"]
    assert isinstance(services, dict)
    api = services["api"]
    writer = services["writeback-worker"]

    assert api["environment"]["STAR_WRITEBACK_ENABLED"] == "true"
    assert "secrets" not in api
    assert writer["environment"]["STAR_WRITEBACK_ENABLED"] == "true"
    assert "secrets" not in writer
    assert writer["env_file"] == [".env"]
    assert "secrets" not in compose
    assert set(writer["networks"]) == {"edge", "data"}
    assert "ports" not in writer
    assert writer["read_only"] is True
    assert writer["cap_drop"] == ["ALL"]


def test_standard_worker_capabilities_fit_the_automatic_enrollment_policy() -> None:
    """A rebuilt standard worker must never reject its own safe re-enrollment."""

    coordinator = _coordinator_compose()
    environment = coordinator["x-star-environment"]
    allowed = set(environment["STAR_WORKER_AUTO_ENROLL_CAPABILITIES"].split(","))
    for compose_path, service_names in {
        ROOT / "Deployment/worker-nvidia/compose.yaml": {"technical-worker", "gpu-worker"},
        ROOT / "Deployment/worker-apple/compose.yaml": {
            "accelerator-worker",
        },
    }.items():
        services = yaml.safe_load(compose_path.read_text(encoding="utf-8"))["services"]
        for service_name in service_names:
            command = services[service_name]["command"]
            capability_index = command.index("--capabilities")
            advertised = {
                value
                for value in command[capability_index + 1].split(",")
                if not value.startswith("${")
            }
            assert advertised <= allowed, (
                f"{compose_path.parent.name}/{service_name} advertises capabilities "
                f"outside coordinator automatic enrollment: {sorted(advertised - allowed)}"
            )


def test_retired_apple_vision_and_intel_sidecar_are_gone() -> None:
    """v209 retired Apple Vision and the Intel Mac sidecar; nothing may reference them."""

    assert not (ROOT / "Deployment/worker-intel-mac").exists()
    for relative in (
        "Deployment/worker-apple/apple-vision-service.py",
        "Deployment/worker-apple/install-apple-vision.sh",
        "Deployment/worker-apple/io.star.apple-vision.plist",
        "Deployment/worker-apple/star-vision",
        "scripts/StarVision.swift",
        "scripts/build-intel-mac-worker-bundle.sh",
    ):
        assert not (ROOT / relative).exists(), relative
    nvidia = yaml.safe_load(
        (ROOT / "Deployment/worker-nvidia/compose.yaml").read_text(encoding="utf-8")
    )["services"]
    nvidia_capabilities = nvidia["technical-worker"]["command"]
    nvidia_capability_index = nvidia_capabilities.index("--capabilities")
    assert nvidia_capabilities[nvidia_capability_index + 1] == "cpu-light,semantic-embedding"
    apple_compose = ROOT / "Deployment/worker-apple/compose.yaml"
    apple = yaml.safe_load(apple_compose.read_text(encoding="utf-8"))[
        "services"
    ]
    assert "apple-vision-worker" not in apple
    assert (ROOT / "Deployment/worker-apple/apple-power-service.py").exists()


def test_star_ships_no_external_observability() -> None:
    """Star keeps its own diagnostics; nothing goes to Grafana, Prometheus or Tempo.

    The owner shut the house observability stack down to give its resources to
    Star. Everything worth knowing is read through Star's own surfaces, so no
    tracing library, metrics exporter, dashboard asset or endpoint may remain.
    """

    compose = _compose()
    assert "grafana" not in compose["services"]
    assert "prometheus" not in compose["services"]
    assert "observability" not in compose["networks"]
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert "opentelemetry" not in pyproject
    assert "prometheus" not in pyproject
    for compose_path in (
        "Deployment/coordinator/compose.yaml",
        "Deployment/worker-nvidia/compose.yaml",
        "Deployment/worker-apple/compose.yaml",
    ):
        assert "OTEL" not in (ROOT / compose_path).read_text(encoding="utf-8"), compose_path
    nginx = (ROOT / "frontend/nginx.conf").read_text(encoding="utf-8")
    assert "_star_grafana_auth" not in nginx
    assert "/api/v1/metrics" not in nginx
    source = ROOT / "backend/src/star"
    assert not (source / "core/tracing.py").exists()
    assert not (source / "api/routers/metrics.py").exists()


def test_vlm_output_contract_has_no_destructive_action_field() -> None:
    schema_text = json.dumps(
        PhotoObservationModelOutput.model_json_schema(), sort_keys=True
    ).lower()
    assert "delete" not in schema_text
    assert "trash" not in schema_text
    assert "tool" not in schema_text


def test_all_current_single_asset_analysis_tasks_can_fetch_their_leased_preview() -> None:
    worker_router = (ROOT / "backend/src/star/api/routers/workers/leasing.py").read_text(
        encoding="utf-8"
    )
    preview_route = worker_router.split('@router.get("/tasks/{task_id}/media")', 1)[1].split(
        '@router.get("/tasks/{task_id}/media/{asset_id}")', 1
    )[0]
    assert "LEASE_TASK_CONTRACTS" in preview_route
    for task_type in (
        "asset.deep_review",
        "asset.primary_review",
        "asset.media_stage",
    ):
        assert task_type in LEASE_TASK_CONTRACTS
    for removed_task_type in (
        "asset.apple_order_check",
        "asset.fast_rating",
        "asset.deep_review_experiment",
        "asset.technical_preview",
        "asset.apple_vision",
    ):
        assert removed_task_type not in LEASE_TASK_CONTRACTS


def test_every_alert_action_link_uses_a_filter_the_gallery_accepts() -> None:
    """An alert whose link answers 422 is worse than no link at all.

    The excluded-photos alert pointed at ?state=excluded, but "excluded" is a
    photo's place in a job, not an analysis state, so opening the alert failed
    with a validation error every time.
    """

    from star.api.routers.assets.gallery import list_gallery_assets
    from star.api.routers.system import FAILURE_GUIDANCE

    # gallery.py uses postponed annotations, so the signature holds strings.
    hints = get_type_hints(list_gallery_assets, include_extras=False)
    analysis_states = set(get_args(hints["analysis_state"]))
    run_states = {value for value in get_args(hints["run_state"]) if isinstance(value, str)}
    for member in get_args(hints["run_state"]):
        run_states.update(get_args(member))

    for error_class, (_message, link, _label) in FAILURE_GUIDANCE.items():
        path, _, raw_query = link.partition("?")
        if path != "/photos" or not raw_query:
            continue
        query = dict(parse_qsl(raw_query))
        if "state" in query:
            assert query["state"] in analysis_states, (
                f"{error_class} uses an unknown analysis state"
            )
        if "run_state" in query:
            assert query["run_state"] in run_states, f"{error_class} uses an unknown job state"
            # A job state only means something relative to a job.
            assert query.get("job"), f"{error_class} filters by job state without naming a job"


def test_curation_diagnostics_report_counts_and_never_content() -> None:
    """The diagnostics that let an operator see the library must stay sanitized.

    This section exists so the star distribution and the reason the final stage
    has not started can be read without shell access to the coordinator. It must
    never become a way to read the photographs themselves.
    """

    from star.api.routers.diagnostics import _curation_diagnostics

    # The docstring promises not to expose filenames, so check the code itself
    # rather than the prose describing it.
    whole = inspect.getsource(_curation_diagnostics)
    source = whole.split('"""')[2] if whole.count('"""') >= 2 else whole
    for forbidden in (
        "filename",
        "original_path",
        "immich_asset_id",
        "summary",
        "prompt",
        "explanation",
        "capture_facts",
    ):
        assert forbidden not in source, f"curation diagnostics must not expose {forbidden}"
    # Counts and codes only.
    assert "func.count()" in source
    assert "by_rating" in source
    assert "highlights_blocked_by" in source


def test_nightly_database_backup_is_verified_and_pruned() -> None:
    """One verified custom-format dump a night at 03:00 UTC, fourteen kept, restorable locally."""

    services = _coordinator_compose()["services"]
    backup = services["database-backup"]
    assert backup["image"] == services["postgres"]["image"]
    assert "${STAR_DATA_DIR:-./star-data}/starml:/starml" in backup["volumes"]
    assert backup["user"] == "10001:10001"
    command = "\n".join(backup["command"])
    assert "pg_dump" in command and "--format custom" in command
    assert "pg_restore --list" in command and "sha256sum" in command
    assert "until_0300" in command and "tail -n +15" in command


def test_workers_publish_no_ports_and_only_web_publishes_on_the_nas() -> None:
    """The coordinator web service is the only entry point; nothing else exposes a port."""

    for machine in ("worker-nvidia", "worker-apple"):
        services = yaml.safe_load(
            (ROOT / f"Deployment/{machine}/compose.yaml").read_text(encoding="utf-8")
        )["services"]
        assert not any(service.get("ports") for service in services.values()), machine
    coordinator = _coordinator_compose()
    services = coordinator["services"]
    assert {name for name, service in services.items() if service.get("ports")} == {"web"}
    assert coordinator["networks"]["data"]["internal"] is True
    assert coordinator["services"]["postgres"]["networks"] == ["data"]
