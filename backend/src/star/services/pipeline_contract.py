from __future__ import annotations

from dataclasses import asdict, dataclass

from star.domain.enums import TaskState

PIPELINE_CONTRACT_VERSION = "progressive-review-v18"
HIGHLIGHT_PROMPT_VERSION = "highlight-compare-visible-focus-and-diversity-v9"

# These sets are the one compatibility boundary for persisted task roles.  Old
# releases used ``core`` and ``enrichment`` while the public contract uses
# ``required`` and ``optional``.  Reads must remain compatible with historical
# rows, but every current producer and status surface uses the canonical role.
REQUIRED_COMPLETION_ROLES = frozenset({"required", "core"})
OPTIONAL_COMPLETION_ROLES = frozenset(
    {
        "optional",
        "enrichment",
        "optional_fallback",
        "conditional",
        "supporting",
        "manual_nonblocking",
        "manual_advisory",
        "administrative",
        "experiment",
    }
)
KNOWN_COMPLETION_ROLES = REQUIRED_COMPLETION_ROLES | OPTIONAL_COMPLETION_ROLES

ACTIVE_TASK_STATES = frozenset(
    {
        TaskState.PLANNED,
        TaskState.QUEUED,
        TaskState.RETRY_WAITING,
        TaskState.RUNNING,
        TaskState.CANCEL_REQUESTED,
    }
)
TERMINAL_TASK_STATES = frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED})


@dataclass(frozen=True)
class TaskContract:
    task_type: str
    stage: str
    route: str
    capabilities: tuple[str, ...]
    model_role: str
    result_schema: str
    data_class: str
    completion_role: str
    queue_stage: str
    label: str
    purpose: str
    resident: bool = False
    machine: str = ""

    def payload(self) -> dict[str, object]:
        value = asdict(self)
        value["capabilities"] = list(self.capabilities)
        return value


@dataclass(frozen=True)
class TaskRuntimeContract:
    """Resource envelope shared by the task producer and worker executor."""

    task_type: str
    candidate_count: int | None = None
    context_tokens: int | None = None
    max_completion_tokens: int | None = None
    max_media_edge: int | None = None
    contact_sheet_edge: int | None = None
    compute_budget_ms: int | None = None
    # Declared degraded envelope for a bounded second attempt after a runtime
    # context-window or memory failure. Absent fields mean no fallback exists
    # and the failure stays terminal for that dimension.
    fallback_context_tokens: int | None = None
    fallback_max_media_edge: int | None = None

    def payload(self) -> dict[str, int]:
        return {
            key: value
            for key, value in asdict(self).items()
            if key != "task_type" and isinstance(value, int)
        }


PRODUCTION_TASKS: tuple[TaskContract, ...] = (
    TaskContract(
        "asset.media_stage",
        "administration",
        "technical",
        ("cpu-light", "technical"),
        "administrative",
        "MediaStageResultSubmission",
        "family-preview",
        "administrative",
        "administration",
        "Media staging",
        "Pre-fetch and normalize upcoming review derivatives into the coordinator "
        "cache so the Immich media disks can rest between staging bursts.",
        machine="NVIDIA GPU worker",
    ),
    TaskContract(
        "asset.semantic_embedding",
        "embedding",
        "semantic-embedding",
        ("semantic-embedding",),
        "similarity_evidence",
        "SemanticEmbeddingResultSubmission",
        "family-preview",
        "supporting",
        "similarity",
        "Similarity embedding",
        "Local similarity evidence used to form comparable series.",
        machine="NVIDIA GPU worker",
    ),
    TaskContract(
        "asset.primary_review",
        "primary",
        "small-vlm",
        ("small-vlm",),
        "primary_rating",
        "StarRatingResultSubmission",
        "family-preview",
        "required",
        "ministral",
        "Primary photo review",
        "The individual 1-5-star proposal from NVIDIA Ministral; the senior review "
        "supersedes it for 4- and 5-star candidates.",
        True,
        "NVIDIA GPU worker",
    ),
    TaskContract(
        "asset.deep_review",
        "advisory",
        "medium-vlm",
        ("medium-vlm", "large-vlm", "mlx"),
        "senior_rating",
        "StarRatingResultSubmission",
        "family-preview",
        "conditional",
        "senior_review",
        "Senior review",
        "Automatic Qwen3.8 27B review of every photo the primary review rated 4 or 5, on "
        "whichever review machine holds the model (Apple MLX or NVIDIA Bonsai). Its star "
        "replaces the primary proposal; it never blocks completion.",
        True,
        "Mac or NVIDIA GPU worker",
    ),
    TaskContract(
        "group.highlight_compare",
        "highlights",
        "small-vlm",
        ("small-vlm",),
        "relative_comparison",
        "HighlightComparisonResultSubmission",
        "family-preview",
        "conditional",
        "highlight_league",
        "Highlight comparison",
        "Blind Qwen3-VL collection-relative comparison.",
        True,
        "NVIDIA GPU worker",
    ),
    TaskContract(
        # The library reading itself once a night. Administrative on purpose: it
        # is never part of any photograph's completion, so a failed or skipped
        # reading can never hold a photograph back.
        "library.review",
        "administration",
        "medium-vlm",
        ("medium-vlm", "large-vlm", "mlx"),
        "administrative",
        "LibraryReviewSubmission",
        "operational-metadata",
        "administrative",
        "administration",
        "Library review",
        "Read the library's own counts and propose the next useful work.",
        False,
        "Local worker",
    ),
    TaskContract(
        "worker.model_warmup",
        "administration",
        "medium-vlm",
        ("small-vlm", "medium-vlm", "large-vlm", "mlx"),
        "administrative",
        "WorkerModelWarmupSubmission",
        "model-metadata",
        "administrative",
        "administration",
        "Model warm-up",
        "Load an active production model before compatible queued work.",
        True,
        "Local worker",
    ),
)

TASK_CONTRACTS = {contract.task_type: contract for contract in PRODUCTION_TASKS}

EXPERIMENT_TASKS: tuple[TaskContract, ...] = (
    TaskContract(
        "group.series_select",
        "experiment",
        "medium-vlm",
        ("medium-vlm", "multi-image-review", "mlx"),
        "experiment_only",
        "SeriesSelectionResultSubmission",
        "family-preview",
        "experiment",
        "experiments",
        "Deep series comparison",
        "Owner-requested Qwen3.8 comparison that chooses one representative per visual series.",
        True,
        "Mac",
    ),
)
EXPERIMENT_TASK_CONTRACTS = {contract.task_type: contract for contract in EXPERIMENT_TASKS}
LEASE_TASK_CONTRACTS = {**TASK_CONTRACTS, **EXPERIMENT_TASK_CONTRACTS}
MULTI_ROUTE_TASK_TYPES: dict[str, frozenset[str]] = {}
# v209 (Part C and round 3): per-photo work that never fed the star. Their rows
# stay readable history; the scheduler cancels queued leftovers once, and every
# lease or completion path refuses them with a clear detail. Round 3 added the
# Technical preview (its catastrophic-blur cap and queue ordering are gone; the
# models see blur themselves) and Apple Vision (Immich already supplies faces
# and names).
RETIRED_TASK_TYPES: frozenset[str] = frozenset(
    {
        "asset.clip_embedding",
        "asset.description_embedding",
        "group.cohort_triage",
        "asset.technical_preview",
        "asset.apple_vision",
    }
)
RETIRED_TASK_DETAIL = "task type was retired in v209 and can no longer be leased or completed"

# Runtime envelopes live next to the task registry so producers, workers,
# documentation, and UI cannot silently choose different image or token limits.
TASK_RUNTIME_CONTRACTS = {
    "asset.media_stage": TaskRuntimeContract(
        task_type="asset.media_stage",
        candidate_count=1,
        max_media_edge=2048,
    ),
    "asset.primary_review": TaskRuntimeContract(
        task_type="asset.primary_review",
        candidate_count=1,
        # Measured on the real prompt: system policy, instruction, rating scale,
        # owner taste, capture/series/technical context and a full-size photo
        # come to about 2,850 tokens, and the answer is capped at 512. 6,144
        # keeps a wide margin while halving the key/value cache that an 8 GiB
        # NVIDIA GPU has to hold beside a 6 GB model; 8,192 forced Ollama to
        # offload part of the model to the CPU, which the worker refuses.
        context_tokens=6144,
        # A hang guard, not a target: StarRatingV1 normally needs about 100
        # tokens and has no prose fields. Keep enough repair headroom without
        # inviting an unnecessarily long generation.
        max_completion_tokens=512,
        max_media_edge=2048,
        compute_budget_ms=180_000,
        fallback_context_tokens=4096,
        fallback_max_media_edge=1440,
    ),
    "group.highlight_compare": TaskRuntimeContract(
        task_type="group.highlight_compare",
        candidate_count=4,
        context_tokens=16_384,
        max_completion_tokens=256,
        max_media_edge=2048,
        contact_sheet_edge=4096,
        compute_budget_ms=150_000,
    ),
    "asset.deep_review": TaskRuntimeContract(
        task_type="asset.deep_review",
        candidate_count=1,
        context_tokens=16_384,
        # The compact answer plus repair headroom; thinking is opt-in through the
        # owner's inference settings and raises this guard from the manifest.
        max_completion_tokens=1024,
        max_media_edge=1440,
        compute_budget_ms=600_000,
    ),
    "group.series_select": TaskRuntimeContract(
        task_type="group.series_select",
        candidate_count=4,
        context_tokens=16_384,
        max_completion_tokens=1_024,
        max_media_edge=1_280,
        compute_budget_ms=180_000,
    ),
}

MLX_LARGE_CONTEXT_TASK_TYPES: frozenset[str] = frozenset()

# Durable marker carried by newly materialized model work. Recovery may replay
# an older task after a release repairs its envelope, but must never reopen a
# deterministic context failure produced by this same contract indefinitely.
MODEL_RUNTIME_CONTRACT_REVISION = "v181-star-rating-v1"

ORDERED_STAGES: tuple[dict[str, object], ...] = (
    {
        "key": "primary",
        "label": "Ministral 3 8B",
        "model_role": "primary_rating",
        "required": True,
    },
    {
        "key": "senior_review",
        "label": "Qwen3.8 27B senior review",
        "model_role": "senior_rating",
        "required": False,
    },
    {
        "key": "final",
        "label": "Individual review publication",
        "model_role": "materialization_and_publication",
        "required": True,
    },
    {
        "key": "highlights",
        "label": "Qwen3-VL Highlight League",
        "model_role": "conditional_relative_comparison",
        "required": False,
    },
)

PRODUCTION_MODELS: tuple[dict[str, str], ...] = (
    {
        "role": "primary_rating",
        "model_key": "ministral-3:8b-instruct-2512-q4_K_M",
        "machine": "nvidia",
    },
    {
        "role": "relative_comparison",
        "model_key": "qwen3-vl:8b-instruct-q4_K_M",
        "machine": "nvidia",
    },
    {
        "role": "senior_rating",
        "model_key": "qwen3.8:27b",
        "machine": "apple+nvidia",
    },
)
PRODUCTION_MODEL_KEYS = {str(model["role"]): str(model["model_key"]) for model in PRODUCTION_MODELS}
EXPERIMENT_MODEL_KEYS = frozenset(
    {
        "gemma4-e4b-vision:q4_0",
        "qwen3.5:9b",
        "gemma-3-27b-it-qat-mlx",
    }
)


def routes_for_capabilities(capabilities: set[str]) -> set[str]:
    return {
        contract.route
        for contract in PRODUCTION_TASKS
        if contract.completion_role != "administrative"
        and capabilities.intersection(contract.capabilities)
    }


def task_types_for_route(route: str) -> set[str]:
    return {contract.task_type for contract in PRODUCTION_TASKS if contract.route == route}


def lease_task_types_for_route(route: str) -> set[str]:
    """Return the only task types a worker may lease for a route.

    Experiment contracts are executable only when an explicit experiment queued
    them; they are deliberately excluded from the public production contract.
    """

    return {
        contract.task_type
        for contract in (*PRODUCTION_TASKS, *EXPERIMENT_TASKS)
        if contract.route == route or route in MULTI_ROUTE_TASK_TYPES.get(contract.task_type, ())
    }


def stage_task_types() -> dict[str, frozenset[str]]:
    stages: dict[str, set[str]] = {}
    for contract in PRODUCTION_TASKS:
        if contract.completion_role in {"administrative", "experiment"}:
            continue
        stages.setdefault(contract.queue_stage, set()).add(contract.task_type)
    return {key: frozenset(value) for key, value in stages.items()}


def canonical_completion_role(task_type: str, persisted_role: str | None = None) -> str:
    contract = TASK_CONTRACTS.get(task_type) or EXPERIMENT_TASK_CONTRACTS.get(task_type)
    if contract is not None:
        return contract.completion_role
    normalized = (persisted_role or "").strip()
    return normalized if normalized in KNOWN_COMPLETION_ROLES else "optional"


def task_type_blocks_completion(task_type: str, persisted_role: str | None = None) -> bool:
    return canonical_completion_role(task_type, persisted_role) in REQUIRED_COMPLETION_ROLES


def validate_pipeline_contract() -> None:
    """Fail startup/build when the released task graph contradicts itself."""

    task_types = [contract.task_type for contract in (*PRODUCTION_TASKS, *EXPERIMENT_TASKS)]
    if len(task_types) != len(set(task_types)):
        raise RuntimeError("pipeline task types must be unique")
    unknown_roles = {
        contract.completion_role
        for contract in (*PRODUCTION_TASKS, *EXPERIMENT_TASKS)
        if contract.completion_role not in KNOWN_COMPLETION_ROLES
    }
    if unknown_roles:
        raise RuntimeError("pipeline contract contains an unknown completion role")
    for task_type, runtime in TASK_RUNTIME_CONTRACTS.items():
        if task_type not in LEASE_TASK_CONTRACTS:
            raise RuntimeError("runtime contract has no executable task contract")
        if runtime.candidate_count is not None and runtime.candidate_count < 1:
            raise RuntimeError("runtime contract candidate count must be positive")
        if runtime.context_tokens is not None:
            context_ceiling = 196_608 if task_type in MLX_LARGE_CONTEXT_TASK_TYPES else 32_768
            if not 1024 <= runtime.context_tokens <= context_ceiling:
                raise RuntimeError("runtime contract context is outside the local safety range")
        if (
            runtime.max_completion_tokens is not None
            and runtime.context_tokens is not None
            and runtime.max_completion_tokens >= runtime.context_tokens
        ):
            raise RuntimeError("runtime output allowance must be smaller than its context")
        if runtime.max_media_edge is not None and not 256 <= runtime.max_media_edge <= 2048:
            raise RuntimeError("runtime media edge is outside the released derivative envelope")
        if runtime.contact_sheet_edge is not None and not 512 <= runtime.contact_sheet_edge <= 4096:
            raise RuntimeError("runtime contact sheet is outside the released comparison envelope")
        if (
            runtime.contact_sheet_edge is not None
            and runtime.max_media_edge is not None
            and runtime.candidate_count is not None
            and runtime.candidate_count >= 4
            and runtime.contact_sheet_edge < runtime.max_media_edge * 2
        ):
            raise RuntimeError("runtime contact sheet cannot preserve four candidate inputs")


def worker_stage_keys() -> frozenset[str]:
    """DAG stages that represent measured worker compute."""

    return frozenset(
        contract.stage
        for contract in PRODUCTION_TASKS
        if contract.completion_role not in {"administrative", "experiment"}
    )


def pipeline_contract_payload() -> dict[str, object]:
    return {
        "version": PIPELINE_CONTRACT_VERSION,
        "stages": list(ORDERED_STAGES),
        "models": list(PRODUCTION_MODELS),
        "tasks": [
            {
                **contract.payload(),
                "runtime": (
                    TASK_RUNTIME_CONTRACTS[contract.task_type].payload()
                    if contract.task_type in TASK_RUNTIME_CONTRACTS
                    else None
                ),
            }
            for contract in PRODUCTION_TASKS
        ],
    }
