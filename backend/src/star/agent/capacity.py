from __future__ import annotations

import os
from pathlib import Path


def _capabilities(values: str) -> dict[str, bool]:
    capabilities = {item.strip(): True for item in values.split(",") if item.strip()}
    if not capabilities:
        raise ValueError("at least one capability is required")
    return capabilities


def _read_cgroup_number(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if value == "max":
        return None
    try:
        number = int(value)
    except ValueError:
        return None
    return number if number >= 0 else None


def _proc_memory() -> tuple[int, int] | None:
    try:
        fields = {
            key: int(value.split()[0]) * 1024
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines()
            if ":" in line
            for key, value in [line.split(":", 1)]
        }
    except OSError, ValueError, IndexError:
        return None
    total = fields.get("MemTotal")
    available = fields.get("MemAvailable")
    if total is None or available is None or total <= 0:
        return None
    return total, min(total, available)


def _memory_capacity() -> tuple[int, int] | None:
    cgroup_candidates = (
        (Path("/sys/fs/cgroup/memory.max"), Path("/sys/fs/cgroup/memory.current")),
        (
            Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
            Path("/sys/fs/cgroup/memory/memory.usage_in_bytes"),
        ),
    )
    host_memory = _proc_memory()
    for limit_path, usage_path in cgroup_candidates:
        limit = _read_cgroup_number(limit_path)
        usage = _read_cgroup_number(usage_path)
        if limit is None or usage is None or limit <= 0:
            continue
        # Some cgroup-v1 hosts expose an effectively unlimited sentinel. Prefer
        # actual host memory in that case rather than reporting exabytes.
        if host_memory is not None and limit > host_memory[0] * 2:
            continue
        return limit, max(0, limit - min(limit, usage))
    return host_memory


def _cpu_capacity() -> tuple[int, float]:
    logical_cpus = max(1, os.cpu_count() or 1)
    quota_cpus: float | None = None
    try:
        quota_value, period_value = (
            Path("/sys/fs/cgroup/cpu.max").read_text(encoding="utf-8").split()[:2]
        )
        if quota_value != "max":
            quota = int(quota_value)
            period = int(period_value)
            if quota > 0 and period > 0:
                quota_cpus = quota / period
    except OSError, ValueError, IndexError:
        legacy_quota = _read_cgroup_number(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us"))
        legacy_period = _read_cgroup_number(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us"))
        if (
            legacy_quota is not None
            and legacy_period is not None
            and legacy_quota > 0
            and legacy_period > 0
        ):
            quota_cpus = legacy_quota / legacy_period
    available_cpus = max(0.01, min(float(logical_cpus), quota_cpus or logical_cpus))
    return logical_cpus, available_cpus


def _runtime_load(active_jobs: int, maximum_concurrency: int) -> dict[str, float]:
    logical_cpus, available_cpus = _cpu_capacity()
    try:
        load_1m, load_5m, load_15m = os.getloadavg()
    except OSError:
        load_1m = load_5m = load_15m = 0.0
    values = {
        "active_jobs": float(active_jobs),
        "capacity": float(maximum_concurrency),
        "logical_cpus": float(logical_cpus),
        "cpu_limit": available_cpus,
        "load_1m": float(load_1m),
        "load_5m": float(load_5m),
        "load_15m": float(load_15m),
        "load_percent": min(100.0, max(0.0, load_1m / available_cpus * 100.0)),
    }
    memory = _memory_capacity()
    if memory is not None:
        total, available = memory
        used = max(0, total - available)
        values.update(
            {
                "memory_total_bytes": float(total),
                "memory_available_bytes": float(available),
                "memory_used_bytes": float(used),
                "memory_percent": used / total * 100.0,
            }
        )
    gpu = _gpu_metrics()
    if gpu is not None:
        values.update(gpu)
    return values


def _gpu_metrics() -> dict[str, float] | None:
    import subprocess

    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw,power.limit,fan.speed,clocks.current.sm",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        parts = [p.strip() for p in result.stdout.strip().split(",")]
        if len(parts) < 6:
            return None

        def parse(v: str) -> float | None:
            try:
                return float(v)
            except ValueError, TypeError:
                return None

        metrics: dict[str, float] = {}
        temp = parse(parts[0])
        if temp is not None:
            metrics["gpu_temperature_c"] = temp
        util = parse(parts[1])
        if util is not None:
            metrics["gpu_utilization_percent"] = util
        mem_used = parse(parts[2])
        if mem_used is not None:
            metrics["gpu_memory_used_mb"] = mem_used
        mem_total = parse(parts[3])
        if mem_total is not None:
            metrics["gpu_memory_total_mb"] = mem_total
        if mem_used is not None and mem_total is not None and mem_total > 0:
            metrics["gpu_memory_percent"] = mem_used / mem_total * 100.0
        power = parse(parts[4])
        if power is not None:
            metrics["gpu_power_draw_watts"] = power
        power_limit = parse(parts[5])
        if power_limit is not None:
            metrics["gpu_power_limit_watts"] = power_limit
        fan = parse(parts[6]) if len(parts) > 6 else None
        if fan is not None:
            metrics["gpu_fan_percent"] = fan
        clock = parse(parts[7]) if len(parts) > 7 else None
        if clock is not None:
            metrics["gpu_clock_mhz"] = clock
        return metrics if metrics else None
    except FileNotFoundError, OSError, subprocess.TimeoutExpired:
        return None
