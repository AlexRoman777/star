from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMINISTRATOR = "administrator"
    CURATOR = "curator"
    REVIEWER = "reviewer"
    READ_ONLY = "read_only"


class CredentialKind(StrEnum):
    READ = "read"
    WRITE = "write"
    TAG_WRITE = "tag_write"


class AccountPurpose(StrEnum):
    MOBILE = "mobile"
    GOPRO = "gopro"
    DJI = "dji"
    NIKON = "nikon"
    SONY = "sony"
    RAW = "raw"
    OTHER = "other"


class HealthStatus(StrEnum):
    UNKNOWN = "unknown"
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    DISABLED = "disabled"


class MediaType(StrEnum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    OTHER = "other"


class CampaignState(StrEnum):
    DRAFT = "draft"
    SCOPING = "scoping"
    ANALYZING = "analyzing"
    REVIEWING = "reviewing"
    PAUSED = "paused"
    COMPLETE = "complete"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"


class LibraryProgramState(StrEnum):
    DISABLED = "disabled"
    RUNNING = "running"
    PAUSED = "paused"
    DRAINING = "draining"
    ERROR = "error"


class ProcessingBatchState(StrEnum):
    PLANNED = "planned"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CandidateBreadth(StrEnum):
    CONSERVATIVE = "conservative"
    BALANCED = "balanced"
    BROAD = "broad"


class RawPolicy(StrEnum):
    NEVER_SUGGEST = "never_suggest"
    STRONGLY_PROTECTED = "strongly_protected"
    CAMERA_POLICY = "camera_policy"


class GroupType(StrEnum):
    EXACT_BINARY = "exact_binary"
    SAME_DECODED = "same_decoded"
    RESIZED = "resized"
    RECOMPRESSED = "recompressed"
    EDITED = "edited"
    RAW_JPEG = "raw_jpeg"
    BURST = "burst"
    SIMILAR_POSE = "similar_pose"
    SAME_SCENE = "same_scene"
    SCREENSHOT_COPY = "screenshot_copy"
    SOCIAL_EXPORT = "social_export"
    VIDEO_FRAME = "video_frame"
    PANORAMA_COMPONENT = "panorama_component"
    HDR_BRACKET = "hdr_bracket"
    FOCUS_BRACKET = "focus_bracket"
    VIDEO_CONTINUATION = "video_continuation"


class ProposalType(StrEnum):
    RATING = "rating"
    KEEP = "keep"
    CLEANUP_CANDIDATE = "cleanup_candidate"
    BEST_OUTPUT = "best_output"
    UNCERTAIN = "uncertain"
    TAG = "tag"


class ProposalState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DISMISSED = "dismissed"
    SUPERSEDED = "superseded"


class DecisionType(StrEnum):
    RATE = "rate"
    SENSITIVE = "sensitive"
    KEEP = "keep"
    PROTECT = "protect"
    CLEANUP_CANDIDATE = "cleanup_candidate"
    BEST_OUTPUT = "best_output"
    UNCERTAIN = "uncertain"
    EXCLUDE = "exclude"
    UNDO = "undo"


class WritebackState(StrEnum):
    DRAFT = "draft"
    PREVIEWED = "previewed"
    APPROVED = "approved"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    CONFLICT = "conflict"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskState(StrEnum):
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAITING = "retry_waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELLED = "cancelled"
