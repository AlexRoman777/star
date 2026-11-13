#!/usr/bin/env python3
"""Fetch Star's sanitized read-only diagnostics or review evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


def _load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _integer(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _progress_summary(
    payload: dict[str, object],
    *,
    previous: dict[str, object] | None = None,
) -> str:
    queue = _mapping(payload.get("queue"))
    states = _mapping(queue.get("by_state"))
    workers = _mapping(payload.get("workers"))
    reanalysis = _mapping(payload.get("reanalysis"))
    lines = [
        f"Star {payload.get('release_version', 'unknown')} · "
        f"{payload.get('status', 'unknown')}",
        str(payload.get("summary") or "No status summary."),
    ]
    if not states or _mapping(payload.get("database")).get("status") == "degraded_snapshot":
        lines.append("Snapshot incomplete; queue, workers, and throughput are unavailable.")
        return "\n".join(lines)
    if reanalysis:
        requested = _integer(reanalysis.get("requested_photos"))
        published = _integer(reanalysis.get("published_photos"))
        failed = _integer(reanalysis.get("failed_photos"))
        excluded = _integer(reanalysis.get("excluded_photos"))
        scanned = _integer(reanalysis.get("scanned_photos"))
        materialized = _integer(reanalysis.get("materialized_photos"))
        lines.append(
            "Re-review: "
            f"{published:,}/{requested:,} published · {scanned:,} scanned · "
            f"{materialized:,} materialized · {failed:,} unresolved failures · "
            f"{excluded:,} excluded · {_integer(reanalysis.get('active_tasks')):,} active tasks"
        )
    lines.append(
        "Queue now: "
        f"{_integer(queue.get('active')):,} active · "
        f"{_integer(states.get('running')):,} running · "
        f"{_integer(states.get('retry_waiting')):,} retrying"
    )
    lines.append(
        "Workers: "
        f"{_integer(workers.get('online'))}/{_integer(workers.get('enrolled'))} online · "
        f"{_integer(workers.get('primary_review_runnable'))} Primary-capable"
    )
    historical_failed = _integer(states.get("failed"))
    historical_cancelled = _integer(states.get("cancelled"))
    lines.append(
        "History only: "
        f"{historical_failed:,} failed task rows · {historical_cancelled:,} cancelled task rows"
    )
    if previous is not None and _mapping(_mapping(previous.get("queue")).get("by_state")):
        current_at = _timestamp(payload.get("generated_at"))
        previous_at = _timestamp(previous.get("generated_at"))
        elapsed_hours = (
            (current_at - previous_at).total_seconds() / 3600
            if current_at is not None and previous_at is not None and current_at > previous_at
            else 0.0
        )
        previous_queue = _mapping(previous.get("queue"))
        previous_states = _mapping(previous_queue.get("by_state"))
        succeeded_delta = _integer(states.get("succeeded")) - _integer(
            previous_states.get("succeeded")
        )
        previous_reanalysis = _mapping(previous.get("reanalysis"))
        active_delta = _integer(queue.get("active")) - _integer(
            previous_queue.get("active")
        )
        if elapsed_hours > 0:
            comparison = (
                "Since comparison: "
                f"{succeeded_delta:+,} accepted tasks "
                f"({succeeded_delta / elapsed_hours:,.1f}/h) · "
                f"{active_delta:+,} active tasks"
            )
            same_reanalysis = bool(
                reanalysis
                and previous_reanalysis
                and reanalysis.get("created_at") == previous_reanalysis.get("created_at")
            )
            if same_reanalysis:
                published_delta = _integer(reanalysis.get("published_photos")) - _integer(
                    previous_reanalysis.get("published_photos")
                )
                comparison += (
                    f" · {published_delta:+,} published photos "
                    f"({published_delta / elapsed_hours:,.1f}/h)"
                )
            lines.append(comparison)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reviews",
        action="store_true",
        help="Fetch current-generation review evidence instead of operational diagnostics.",
    )
    parser.add_argument(
        "--all-reviews",
        action="store_true",
        help="Fetch every paginated current-generation review into one local JSON document.",
    )
    parser.add_argument(
        "--preferences",
        action="store_true",
        help="Fetch current owner preference labels through the same scoped read-only key.",
    )
    parser.add_argument(
        "--all-preferences",
        action="store_true",
        help="Fetch every paginated owner preference label into one private local document.",
    )
    parser.add_argument(
        "--dismiss-reanalysis-failures",
        action="store_true",
        help="Accept the photographs the current re-analysis could never finish; idempotent.",
    )
    parser.add_argument(
        "--prune-history",
        action="store_true",
        help="Remove superseded generations and cancelled tasks older than three days; one batch.",
    )
    parser.add_argument("--asset-id", help="Return one opaque Star asset ID.")
    parser.add_argument(
        "--trace",
        metavar="ASSET_ID",
        help="Trace one photograph: every generation, node, task and vote, as states.",
    )
    parser.add_argument(
        "--stream",
        action="store_true",
        help="Tail sanitized operational events live (Ctrl-C to stop).",
    )
    parser.add_argument("--after", help="Continue review pagination after this asset ID.")
    parser.add_argument("--limit", type=int, default=100, help="Review page size (1-500).")
    parser.add_argument(
        "--output",
        type=Path,
        help="Write private output to this local file with owner-only permissions.",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Print a concise operational and re-review progress summary.",
    )
    parser.add_argument(
        "--retry-failed-reanalysis",
        action="store_true",
        help=(
            "Use the separate operations credential to retry failed work "
            "in the latest re-analysis."
        ),
    )
    parser.add_argument(
        "--ensure-reanalysis-complete",
        action="store_true",
        help="Resume the current re-analysis and retry its failed work.",
    )
    parser.add_argument(
        "--pause-reanalysis",
        action="store_true",
        help="Pause new work in the current re-analysis.",
    )
    parser.add_argument(
        "--resume-reanalysis",
        action="store_true",
        help="Resume new work in the current re-analysis.",
    )
    parser.add_argument(
        "--compare",
        type=Path,
        help="With --progress, compare against an earlier saved diagnostics snapshot.",
    )
    args = parser.parse_args()
    if args.all_reviews:
        args.reviews = True
    if args.all_preferences:
        args.preferences = True
    if args.reviews and args.preferences:
        parser.error("choose either review evidence or preference labels")
    if args.progress and (args.reviews or args.preferences):
        parser.error("--progress cannot be combined with review or preference evidence")
    operation_flags = {
        "retry_failed": args.retry_failed_reanalysis,
        "ensure_complete": args.ensure_reanalysis_complete,
        "pause": args.pause_reanalysis,
        "resume": args.resume_reanalysis,
        "dismiss_failures": args.dismiss_reanalysis_failures,
        "prune_history": args.prune_history,
    }
    selected_operations = [name for name, selected in operation_flags.items() if selected]
    if len(selected_operations) > 1:
        parser.error("choose only one operations action")
    if selected_operations and (
        args.progress or args.reviews or args.preferences or args.compare or args.output
    ):
        parser.error("operations actions must be used by themselves")
    if args.compare is not None and not args.progress:
        parser.error("--compare requires --progress")
    if not 1 <= args.limit <= 500:
        parser.error("--limit must be between 1 and 500")
    if (args.asset_id or args.after) and not (args.reviews or args.preferences):
        parser.error("--asset-id and --after require --reviews or --preferences")
    if (args.all_reviews or args.all_preferences) and (args.asset_id or args.after):
        parser.error("all-page modes cannot be combined with --asset-id or --after")

    local = _load_env(Path(".env.agent"))
    base_url = os.environ.get(
        "STAR_AGENT_URL",
        local.get("STAR_AGENT_URL", "http://192.168.0.10:3822"),
    ).rstrip("/")
    token = os.environ.get(
        "STAR_AGENT_DIAGNOSTICS_TOKEN",
        local.get("STAR_AGENT_DIAGNOSTICS_TOKEN", ""),
    )
    operations_token = os.environ.get(
        "STAR_AGENT_OPERATIONS_TOKEN",
        local.get("STAR_AGENT_OPERATIONS_TOKEN", ""),
    )
    selected_token = operations_token if selected_operations else token
    if len(selected_token) < 32:
        print(
            "Configure STAR_AGENT_OPERATIONS_TOKEN in the ignored .env.agent file."
            if selected_operations
            else "Configure STAR_AGENT_DIAGNOSTICS_TOKEN in the ignored .env.agent file.",
            file=sys.stderr,
        )
        return 2
    if urlparse(base_url).scheme not in {"http", "https"}:
        print("STAR_AGENT_URL must use http or https.", file=sys.stderr)
        return 2

    def fetch(endpoint: str, *, method: str = "GET") -> dict[str, object]:
        request = Request(  # noqa: S310 - scheme is explicitly restricted above
            f"{base_url}{endpoint}",
            data=b"" if method == "POST" else None,
            method=method,
            headers={
                "Authorization": f"Bearer {selected_token}",
                "Accept": "application/json",
            },
        )
        # nosemgrep: dynamic-urllib-use-detected -- the URL is validated as HTTP(S) above
        with urlopen(request, timeout=30) as response:  # noqa: S310 - validated HTTP(S)
            value = json.load(response)
        if not isinstance(value, dict):
            raise ValueError("Star returned an invalid diagnostics document")
        return value

    try:
        endpoint = "/api/v1/agent/diagnostics"
        if args.stream:
            request = Request(  # noqa: S310 - scheme is explicitly restricted above
                f"{base_url}/api/v1/agent/events/stream",
                headers={"Authorization": f"Bearer {selected_token}", "Accept": "text/event-stream"},
            )
            # nosemgrep: dynamic-urllib-use-detected -- the URL is validated as HTTP(S) above
            with urlopen(request, timeout=None) as response:  # noqa: S310
                for raw in response:
                    line = raw.decode("utf-8", "replace").rstrip("\n")
                    if line.startswith("data: "):
                        print(line[6:], flush=True)
            return 0
        if args.trace:
            payload = fetch(f"/api/v1/agent/trace/{args.trace}")
        elif selected_operations:
            operation_paths = {
                "retry_failed": "/api/v1/agent/operations/reanalysis/retry-failed",
                "ensure_complete": "/api/v1/agent/operations/reanalysis/ensure-complete",
                "pause": "/api/v1/agent/operations/reanalysis/pause",
                "stop": "/api/v1/agent/operations/reanalysis/stop",
                "dismiss_failures": "/api/v1/agent/operations/reanalysis/dismiss-failures",
                "prune_history": "/api/v1/agent/operations/history/prune",
                "resume": "/api/v1/agent/operations/reanalysis/resume",
            }
            payload = fetch(operation_paths[selected_operations[0]], method="POST")
        elif args.all_reviews or args.all_preferences:
            items: list[object] = []
            after: str | None = None
            surface = "preferences" if args.all_preferences else "reviews"
            while True:
                query = {"limit": args.limit}
                if after is not None:
                    query["after"] = after
                page = fetch(f"/api/v1/agent/{surface}?{urlencode(query)}")
                page_items = page.get("items")
                if not isinstance(page_items, list):
                    raise ValueError("Star returned an invalid review page")
                items.extend(page_items)
                next_after = page.get("next_after")
                if not isinstance(next_after, str) or not next_after:
                    break
                after = next_after
            payload: dict[str, object] = {
                "count": len(items),
                "items": items,
                "privacy": page.get("privacy", "Private local Star evidence."),
            }
        elif args.reviews or args.preferences:
            endpoint = "/api/v1/agent/preferences" if args.preferences else "/api/v1/agent/reviews"
            query = {"limit": args.limit}
            if args.asset_id:
                query["asset_id"] = args.asset_id
            if args.after:
                query["after"] = args.after
            payload = fetch(f"{endpoint}?{urlencode(query)}")
        else:
            payload = fetch(endpoint)
        serialized = json.dumps(payload, indent=2, sort_keys=True)
    except HTTPError as exc:
        print(f"Star agent API returned HTTP {exc.code}.", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"Star agent API is unreachable: {exc.reason}", file=sys.stderr)
        return 1
    previous: dict[str, object] | None = None
    if args.compare is not None:
        try:
            comparison_value = json.loads(args.compare.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Cannot read comparison snapshot: {exc}", file=sys.stderr)
            return 2
        if not isinstance(comparison_value, dict):
            print("Comparison snapshot must be a JSON object.", file=sys.stderr)
            return 2
        previous = comparison_value
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(f"{serialized}\n", encoding="utf-8")
        args.output.chmod(0o600)
        if not args.progress:
            print(f"Wrote {payload.get('count', 'one')} private records to {args.output}.")
    if args.progress:
        print(_progress_summary(payload, previous=previous))
        if args.output is not None:
            print(f"Snapshot saved to {args.output}.")
    elif args.output is None:
        print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
