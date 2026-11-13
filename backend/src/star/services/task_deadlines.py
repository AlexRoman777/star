"""Wall-clock lease guards, distinct from successful photo-compute budgets."""


def task_wall_seconds(task_type: str) -> int | None:
    return {
        "asset.primary_review": 360,
        "asset.deep_review": 1500,
        "group.highlight_compare": 420,
        "asset.media_stage": 180,
    }.get(task_type)
