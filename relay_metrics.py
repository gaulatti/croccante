"""Bounded Prometheus exposition for the Croccante relay container."""

from __future__ import annotations

import math
import os
import re
from collections import defaultdict
from pathlib import Path

STATE_DIR = Path(os.environ.get("STATE_DIR", "/run/croccante"))
BUILD_VERSION = os.environ.get("CROCCANTE_BUILD_VERSION", "development")

DESTINATION_STATES = frozenset(
    {"idle", "waiting", "relaying", "filler", "backoff", "unknown"}
)
CONTROL_METHODS = frozenset({"GET", "POST", "PUT"})
CONTROL_ROUTES = frozenset({"metrics", "session", "filler", "start", "stop", "unknown"})
CONTROL_RESULTS = frozenset(
    {
        "success",
        "unauthorized",
        "invalid",
        "conflict",
        "not_found",
        "error",
        "unknown",
    }
)
RESULT_NAMES = ("transition", "failure", "interrupted")
SLOT_LABEL_LIMIT = 20
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

_control_counts: dict[tuple[str, str, str], int] = defaultdict(int)
_control_duration_sums: dict[tuple[str, str, str], float] = defaultdict(float)


def _read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return default


def _read_nonnegative_int(path: Path, default: int = 0) -> int:
    try:
        return max(0, int(_read_text(path, str(default))))
    except ValueError:
        return default


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(values: dict[str, str]) -> str:
    if not values:
        return ""
    rendered = ",".join(
        f'{key}="{_escape_label(value)}"' for key, value in sorted(values.items())
    )
    return "{" + rendered + "}"


def _sample(name: str, value: int | float, **labels: str) -> str:
    if not math.isfinite(float(value)):
        value = 0
    return f"{name}{_labels(labels)} {value}"


def _metric(lines: list[str], name: str, help_text: str, metric_type: str) -> None:
    lines.extend((f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}"))


def _bounded(value: str, allowed: frozenset[str]) -> str:
    return value if value in allowed else "unknown"


def _slot_label(index: int) -> str:
    return str(index) if 1 <= index <= SLOT_LABEL_LIMIT else "overflow"


def record_control_request(
    method: str, route: str, result: str, duration_seconds: float
) -> None:
    """Record one control or metrics request using controlled label values."""

    key = (
        _bounded(method, CONTROL_METHODS),
        _bounded(route, CONTROL_ROUTES),
        _bounded(result, CONTROL_RESULTS),
    )
    _control_counts[key] += 1
    _control_duration_sums[key] += max(0.0, duration_seconds)


def _process_metrics(lines: list[str]) -> None:
    ticks = os.sysconf("SC_CLK_TCK")
    stat_fields = _read_text(Path("/proc/1/stat")).split()
    cpu_seconds = 0.0
    if len(stat_fields) > 15 and ticks:
        try:
            cpu_seconds = (int(stat_fields[13]) + int(stat_fields[14])) / ticks
        except ValueError:
            cpu_seconds = 0.0

    resident_bytes = 0
    for line in _read_text(Path("/proc/1/status")).splitlines():
        if line.startswith("VmRSS:"):
            try:
                resident_bytes = int(line.split()[1]) * 1024
            except (IndexError, ValueError):
                resident_bytes = 0
            break

    try:
        open_fds = len(tuple(Path("/proc/1/fd").iterdir()))
    except OSError:
        open_fds = 0

    _metric(
        lines,
        "croccante_process_cpu_seconds_total",
        "Total CPU time consumed by container PID 1.",
        "counter",
    )
    lines.append(_sample("croccante_process_cpu_seconds_total", cpu_seconds))
    _metric(
        lines,
        "croccante_process_resident_memory_bytes",
        "Resident memory used by container PID 1.",
        "gauge",
    )
    lines.append(_sample("croccante_process_resident_memory_bytes", resident_bytes))
    _metric(
        lines,
        "croccante_process_open_fds",
        "Open file descriptors held by container PID 1.",
        "gauge",
    )
    lines.append(_sample("croccante_process_open_fds", open_fds))


def collect() -> str:
    """Return the current Croccante state as Prometheus text format."""

    lines: list[str] = []
    version = BUILD_VERSION if VERSION_PATTERN.fullmatch(BUILD_VERSION) else "unknown"
    _metric(lines, "croccante_build_info", "Croccante service build identity.", "gauge")
    lines.append(
        _sample("croccante_build_info", 1, service="croccante", version=version)
    )
    _process_metrics(lines)

    requested = _bounded(
        _read_text(STATE_DIR / "control" / "requested.state", "unknown"),
        frozenset({"started", "stopped", "unknown"}),
    )
    _metric(
        lines,
        "croccante_session_requested_state",
        "Whether the bounded requested broadcast session state is active.",
        "gauge",
    )
    lines.append(_sample("croccante_session_requested_state", 1, state=requested))

    publisher_connected = int((STATE_DIR / "hooks" / "publisher").exists())
    publisher_seen = int((STATE_DIR / "hooks" / "publisher.seen").exists())
    _metric(
        lines,
        "croccante_ingest_publisher_connected",
        "Whether nginx has signalled a currently connected publisher.",
        "gauge",
    )
    lines.append(_sample("croccante_ingest_publisher_connected", publisher_connected))
    _metric(
        lines,
        "croccante_ingest_publisher_seen",
        "Whether the active session has carried publisher media.",
        "gauge",
    )
    lines.append(_sample("croccante_ingest_publisher_seen", publisher_seen))
    for event in ("connect", "disconnect"):
        metric_name = "croccante_ingest_publisher_events_total"
        if event == "connect":
            _metric(
                lines,
                metric_name,
                "Total bounded publisher lifecycle events.",
                "counter",
            )
        value = _read_nonnegative_int(
            STATE_DIR / "metrics" / "hooks" / f"publisher-{event}.total"
        )
        lines.append(_sample(metric_name, value, event=event))

    destination_count = _read_nonnegative_int(STATE_DIR / "dest.count")
    state_counts: dict[tuple[str, str], int] = defaultdict(int)
    active_count = 0
    filler_count = 0
    stalled_count = 0
    for index in range(1, destination_count + 1):
        state = _bounded(
            _read_text(STATE_DIR / f"dest-{index}.state", "unknown"),
            DESTINATION_STATES,
        )
        slot = _slot_label(index)
        state_counts[(slot, state)] += 1
        active_count += int(state in {"relaying", "filler"})
        filler_count += int(state == "filler")
        stalled_count += int(state == "backoff")

    for name, help_text, value in (
        (
            "croccante_relay_destinations_configured",
            "Number of configured relay destinations.",
            destination_count,
        ),
        (
            "croccante_relay_destinations_active",
            "Number of destinations currently relaying live or filler media.",
            active_count,
        ),
        (
            "croccante_filler_destinations_active",
            "Number of destinations currently receiving filler media.",
            filler_count,
        ),
        (
            "croccante_relay_destinations_stalled",
            "Number of destinations currently in retry backoff.",
            stalled_count,
        ),
    ):
        _metric(lines, name, help_text, "gauge")
        lines.append(_sample(name, value))

    _metric(
        lines,
        "croccante_relay_slot_state",
        "Current bounded relay state count for a destination slot.",
        "gauge",
    )
    for (slot, state), value in sorted(state_counts.items()):
        lines.append(
            _sample("croccante_relay_slot_state", value, slot=slot, state=state)
        )

    for metric_name, help_text, suffix in (
        (
            "croccante_relay_attempts_total",
            "Total relay or filler process attempts.",
            "attempt.total",
        ),
        (
            "croccante_relay_retries_total",
            "Total relay retries after fast failures.",
            "retry.total",
        ),
        (
            "croccante_filler_activations_total",
            "Total transitions into filler mode.",
            "filler-activation.total",
        ),
    ):
        _metric(lines, metric_name, help_text, "counter")
        slot_values: dict[str, int] = defaultdict(int)
        for index in range(1, destination_count + 1):
            slot_values[_slot_label(index)] += _read_nonnegative_int(
                STATE_DIR / "metrics" / f"dest-{index}.{suffix}"
            )
        for slot, value in sorted(slot_values.items()):
            lines.append(_sample(metric_name, value, slot=slot))

    _metric(
        lines,
        "croccante_relay_results_total",
        "Total relay attempt results by bounded class.",
        "counter",
    )
    result_values: dict[tuple[str, str], int] = defaultdict(int)
    for index in range(1, destination_count + 1):
        for result in RESULT_NAMES:
            result_values[(_slot_label(index), result)] += _read_nonnegative_int(
                STATE_DIR / "metrics" / f"dest-{index}.result-{result}.total"
            )
    for (slot, result), value in sorted(result_values.items()):
        lines.append(
            _sample(
                "croccante_relay_results_total",
                value,
                slot=slot,
                result=result,
            )
        )

    _metric(
        lines,
        "croccante_relay_backoff_seconds",
        "Current retry backoff delay for a destination slot.",
        "gauge",
    )
    backoff_values: dict[str, int] = defaultdict(int)
    for index in range(1, destination_count + 1):
        value = _read_nonnegative_int(
            STATE_DIR / "metrics" / f"dest-{index}.backoff.seconds"
        )
        slot = _slot_label(index)
        backoff_values[slot] = max(backoff_values[slot], value)
    for slot, value in sorted(backoff_values.items()):
        lines.append(_sample("croccante_relay_backoff_seconds", value, slot=slot))

    _metric(
        lines,
        "croccante_control_requests_total",
        "Total private control and scrape requests.",
        "counter",
    )
    _metric(
        lines,
        "croccante_control_request_duration_seconds",
        "Duration of private control and scrape requests.",
        "summary",
    )
    for (method, route, result), count in sorted(_control_counts.items()):
        labels = {"method": method, "route": route, "result": result}
        lines.append(_sample("croccante_control_requests_total", count, **labels))
        lines.append(
            _sample("croccante_control_request_duration_seconds_count", count, **labels)
        )
        lines.append(
            _sample(
                "croccante_control_request_duration_seconds_sum",
                _control_duration_sums[(method, route, result)],
                **labels,
            )
        )

    return "\n".join(lines) + "\n"
