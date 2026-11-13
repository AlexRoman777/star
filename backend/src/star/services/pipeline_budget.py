from __future__ import annotations

# A cold 8 GB model load is part of local inference. The photo envelope allows
# twenty cumulative compute minutes, while each visual request keeps its own
# realistic cap. A slow attempt returns to the durable queue instead of owning
# the serialized model lane for several minutes. Queueing, coordinator downtime,
# and sleep are not charged as compute.
PHOTO_COMPUTE_BUDGET_MS = 1_200_000
PHOTO_COMPUTE_BUDGET_POLICY = "twenty-minute-budget-v3"
TECHNICAL_COMPUTE_BUDGET_MS = 180_000
APPLE_VISION_COMPUTE_BUDGET_MS = 300_000
FAST_RATING_COMPUTE_BUDGET_MS = 90_000
QUICK_GROUP_COMPUTE_BUDGET_MS = 90_000
PRIMARY_REVIEW_COMPUTE_BUDGET_MS = 180_000
GROUP_COMPARISON_COMPUTE_BUDGET_MS = 150_000
APPLE_ADVISORY_COMPUTE_BUDGET_MS = 600_000
