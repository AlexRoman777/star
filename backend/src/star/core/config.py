from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Annotated
from urllib.parse import quote_plus, urlparse

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _read_secret(path: Path | None, fallback: str | None) -> str:
    if path is not None and path.exists():
        return path.read_text(encoding="utf-8").strip()
    if fallback:
        return fallback
    return ""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="STAR_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: str = "development"
    image_tag: str = "dev"
    build_git_sha: str = Field(default="unknown", pattern=r"^(?:unknown|[0-9a-f]{40})$")
    build_tree_state: str = Field(default="unknown", pattern=r"^(?:clean|dirty|unknown)$")
    build_source_fingerprint: str = Field(default="unknown", pattern=r"^(?:unknown|[0-9a-f]{64})$")
    minimum_worker_protocol_version: int = Field(default=2, ge=1, le=100)
    maximum_worker_protocol_version: int = Field(default=2, ge=1, le=100)
    # Worker packages may intentionally outlive a coordinator release. The numeric
    # worker protocol, routes, and runtime contract are the compatibility gate.
    require_matching_worker_release: bool = False
    public_origin: str = "http://localhost:5173"
    log_level: str = "INFO"
    allowed_hosts: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["localhost", "127.0.0.1"]
    )
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:5173"]
    )

    database_url: str | None = None
    database_host: str = "127.0.0.1"
    database_port: int = 5432
    database_name: str = "star"
    database_user: str = "star"
    database_password: str | None = None
    database_password_file: Path | None = None
    database_pool_size: int = Field(default=5, ge=1, le=20)
    database_max_overflow: int = Field(default=2, ge=0, le=20)
    database_pool_timeout_seconds: float = Field(default=10.0, ge=1.0, le=60.0)
    database_pool_recycle_seconds: int = Field(default=900, ge=60, le=86_400)

    encryption_key: str | None = None
    encryption_key_file: Path | None = None
    session_secret: str | None = None
    session_secret_file: Path | None = None
    bootstrap_token: str | None = None
    bootstrap_token_file: Path | None = None
    agent_diagnostics_token: str | None = None
    agent_diagnostics_token_file: Path | None = None
    agent_diagnostics_require_edge: bool = True
    agent_operations_token: str | None = None
    agent_operations_token_file: Path | None = None
    agent_operations_require_edge: bool = True
    artifact_root: Path = Path("./artifacts")
    model_root: Path = Path("./models")
    # Reusable model-normalized derivatives are content-addressed and bounded.
    # Immich remains the source of truth; cache eviction is always recoverable.
    artifact_cache_max_bytes: int = 20 * 1024**3
    artifact_cache_target_bytes: int = 18 * 1024**3
    immich_internal_url: str | None = None
    preview_stream_max_bytes: int = Field(
        default=50 * 1024 * 1024,
        ge=1024 * 1024,
        le=128 * 1024 * 1024,
    )
    preview_stream_concurrency: int = Field(default=2, ge=1, le=16)
    preview_stream_concurrency_max: int = Field(default=6, ge=1, le=16)
    pressure_cpu_high_percent: float = Field(default=85.0, ge=10.0, le=100.0)
    pressure_memory_high_percent: float = Field(default=88.0, ge=10.0, le=100.0)
    pressure_swap_high_percent: float = Field(default=20.0, ge=0.0, le=100.0)
    pressure_event_loop_lag_ms: float = Field(default=250.0, ge=10.0, le=10_000.0)
    pressure_backoff_seconds: float = Field(default=5.0, ge=0.1, le=300.0)
    nas_star_cpu_budget: float = Field(default=5.0, ge=0.1, le=128.0)
    nas_star_memory_budget_bytes: int = Field(
        default=2688 * 1024 * 1024,
        ge=64 * 1024 * 1024,
    )
    immich_delta_sync_interval_seconds: int = Field(default=15 * 60, ge=60, le=86_400)
    # Workers process at most 2048px inputs; 40MP permits high-resolution
    # derivatives while bounding decoder memory against hostile media.
    artifact_max_image_pixels: int = Field(default=40_000_000, ge=4_194_304, le=80_000_000)
    managed_storage_hard_limit_bytes: int = 500 * 1024**3
    managed_storage_warning_ratio: float = 0.80
    storage_min_free_bytes: int = 50 * 1024**3
    storage_inventory_interval_seconds: int = 6 * 60 * 60
    storage_orphan_grace_seconds: int = 24 * 60 * 60
    storage_partial_grace_seconds: int = 60 * 60
    storage_cleanup_max_files: int = 500
    # Media staging pre-fetches upcoming review derivatives into the NVMe cache
    # in concentrated bursts so the Immich media disks can rest between them.
    staging_enabled: bool = False
    staging_window_photos: int = Field(default=50_000, ge=100, le=500_000)
    staging_loop_interval_seconds: int = Field(default=60, ge=60, le=86_400)
    staging_eviction_grace_days: int = Field(default=7, ge=1, le=90)
    scheduler_poll_seconds: float = 2.0
    scheduler_busy_yield_seconds: float = Field(default=0.25, ge=0.05, le=5.0)
    scheduler_task_batch_size: int = 500
    library_metadata_admission_batch_size: int = Field(default=250, ge=10, le=1000)
    scheduler_hot_admission_batch_size: int = Field(default=100, ge=10, le=500)
    scheduler_queue_low_watermark: int = 500
    scheduler_queue_high_watermark: int = 2000
    operations_retention_days: int = 7
    operations_max_rows: int = 100_000
    operations_stream_poll_seconds: float = 1.0
    operations_stream_heartbeat_seconds: float = 15.0
    system_stream_heartbeat_seconds: float = 15.0
    coordinator_name: str = "star-control-plane"
    coordinator_role: str = "coordinator"
    service_heartbeat_interval_seconds: float = 10.0
    service_stale_seconds: int = 45
    worker_stale_seconds: int = 90
    # Five minutes: the worker renews every 15 s while a task runs, so this only
    # bounds how long an abandoned lease can hold a lane.
    task_lease_minutes: int = 5
    automatic_asset_vlm_models: Annotated[list[str], NoDecode] = Field(default_factory=list)
    worker_credential_days: int = 30
    worker_credential_rotation_days: int = 7
    worker_credential_grace_hours: int = 24
    worker_auto_enroll_enabled: bool = False
    worker_auto_enroll_require_edge: bool = True
    # A device key seen for the first time starts paused and receives no work, and so no
    # photographs, until the owner resumes it on the Machines page.
    worker_auto_enroll_start_paused: bool = True
    worker_auto_enroll_max_clock_skew_seconds: int = 300
    worker_auto_enroll_cidrs: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "127.0.0.0/8",
            "10.0.0.0/8",
            "172.16.0.0/12",
            "192.168.0.0/16",
            "::1/128",
            "fc00::/7",
            "fe80::/10",
        ]
    )
    worker_auto_enroll_capabilities: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "apple-vision",
            "apple-silicon",
            "cpu-light",
            "cuda",
            "image-quality",
            "medium-vlm",
            "semantic-embedding",
            "small-vlm",
            "technical",
        ]
    )
    library_program_enable_allowed: bool = False
    writeback_enabled: bool = False
    session_ttl_hours: int = 24 * 30
    challenge_ttl_minutes: int = 5
    local_password_setup_enabled: bool = False

    @field_validator(
        "allowed_hosts",
        "cors_origins",
        "worker_auto_enroll_cidrs",
        "worker_auto_enroll_capabilities",
        "automatic_asset_vlm_models",
        mode="before",
    )
    @classmethod
    def parse_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @property
    def rp_id(self) -> str:
        hostname = urlparse(self.public_origin).hostname
        if not hostname:
            raise ValueError("public_origin must include a hostname")
        return hostname

    @property
    def async_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        password = quote_plus(_read_secret(self.database_password_file, self.database_password))
        return (
            f"postgresql+asyncpg://{quote_plus(self.database_user)}:{password}"
            f"@{self.database_host}:{self.database_port}/{self.database_name}"
        )

    @property
    def alembic_database_url(self) -> str:
        """Escape ConfigParser interpolation while preserving the decoded SQLAlchemy URL."""
        return self.async_database_url.replace("%", "%%")

    def require_encryption_key(self) -> str:
        value = _read_secret(self.encryption_key_file, self.encryption_key)
        if not value:
            raise RuntimeError("Star encryption key is not configured")
        return value

    def require_session_secret(self) -> str:
        value = _read_secret(self.session_secret_file, self.session_secret)
        if not value:
            raise RuntimeError("Star session secret is not configured")
        return value

    def get_bootstrap_token(self) -> str:
        return _read_secret(self.bootstrap_token_file, self.bootstrap_token)

    def get_agent_diagnostics_token(self) -> str:
        return _read_secret(
            self.agent_diagnostics_token_file,
            self.agent_diagnostics_token,
        )

    def get_agent_operations_token(self) -> str:
        return _read_secret(
            self.agent_operations_token_file,
            self.agent_operations_token,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
