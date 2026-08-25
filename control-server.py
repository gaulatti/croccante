#!/usr/bin/env python3
"""Private, authenticated lifecycle API for one Croccante program."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import signal
import time
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

from destination_runtime import DestinationRuntime
from destination_store import (
    DestinationError,
    DestinationSelection,
    parse_selection,
    provider_from_environment,
    resolve_selection,
)
import relay_metrics
from filler_store import FillerStore, PreparationError, canonical_json


STATE_DIR = Path(os.environ.get("STATE_DIR", "/run/croccante"))
CONTROL_DIR = STATE_DIR / "control"
HOOK_DIR = STATE_DIR / "hooks"
PROGRAM_ID = os.environ["PROGRAM_ID"]
TOKEN_FILE = Path(os.environ.get("CONTROL_TOKEN_FILE", "/run/secrets/croccante-control-token"))
CONTROL_BIND = os.environ.get("CONTROL_BIND", "0.0.0.0")
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "8081"))
PROGRAM_PATH = f"/v1/programs/{quote(PROGRAM_ID, safe='')}/session"
FILLER_PATH = f"/v1/programs/{quote(PROGRAM_ID, safe='')}/fillers/"
DESTINATION_PATH = f"/v1/programs/{quote(PROGRAM_ID, safe='')}/destinations/"
FILLER_STORE = FillerStore(Path(os.environ.get("FILLER_STORE_DIR", "/var/lib/croccante/fillers")), PROGRAM_ID)
METRICS = {"success": 0, "failure": 0, "conflict": 0}
DESTINATION_METRICS = {"success": 0, "invalid": 0, "unavailable": 0, "conflict": 0}
COMMAND_LOCK = threading.RLock()
PREPARATION_LOCK = threading.Lock()
DESTINATION_RUNTIME = DestinationRuntime(STATE_DIR, os.environ.get("RELAY_SUPERVISOR", "/usr/local/bin/relay-dest.sh"))
DESTINATION_PROVIDER = None


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_text(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        return default


def atomic_write(path: Path, value: str) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(f"{value}\n", encoding="utf-8")
    os.replace(temporary, path)


def pid_alive(path: Path) -> bool:
    try:
        os.kill(int(read_text(path)), 0)
        return True
    except (ValueError, ProcessLookupError, PermissionError, OSError):
        return False


def requested_state() -> str:
    return read_text(CONTROL_DIR / "requested.state", "stopped")


def destination_state() -> list[dict[str, object]]:
    try:
        count = int(read_text(STATE_DIR / "dest.count", "0"))
    except ValueError:
        count = 0

    destinations = []
    for index in range(1, count + 1):
        mode = read_text(STATE_DIR / f"dest-{index}.state", "unknown")
        supervisor_healthy = pid_alive(STATE_DIR / f"dest-{index}.wrapper.pid")
        ffmpeg_healthy = pid_alive(STATE_DIR / f"dest-{index}.ffmpeg.pid")
        destinations.append(
            {
                "index": index,
                "id": read_text(STATE_DIR / f"dest-{index}.id") or None,
                "mode": mode,
                "supervisorHealthy": supervisor_healthy,
                "publisherProcessHealthy": ffmpeg_healthy,
            }
        )
    return destinations


def load_last_command() -> dict[str, object] | None:
    raw = read_text(CONTROL_DIR / "last-command.json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"result": "invalid-state"}


def current_state() -> dict[str, object]:
    requested = requested_state()
    destinations = destination_state()
    publisher_connected = (HOOK_DIR / "publisher").exists()
    publisher_seen = (HOOK_DIR / "publisher.seen").exists()
    ffmpeg_running = any(item["publisherProcessHealthy"] for item in destinations)
    wrappers_healthy = bool(destinations) and all(item["supervisorHealthy"] for item in destinations)
    modes = {str(item["mode"]) for item in destinations}

    if requested == "stopped":
        mode = "stopping" if ffmpeg_running or any(item["mode"] != "idle" for item in destinations) else "idle"
        actual = "stopping" if mode == "stopping" else "stopped"
    elif not wrappers_healthy:
        mode = "failed"
        actual = "failed"
    elif publisher_connected:
        mode = "live" if "relaying" in modes else "failed" if modes == {"backoff"} else "waiting-for-publisher"
        actual = "failed" if mode == "failed" else "started"
    elif publisher_seen:
        mode = "filler" if "filler" in modes else "failed" if modes == {"backoff"} else "waiting-for-publisher"
        actual = "failed" if mode == "failed" else "started"
    else:
        mode = "waiting-for-publisher"
        actual = "started"

    active_filler = read_text(CONTROL_DIR / "active-filler.version")
    active_destinations = DESTINATION_RUNTIME.active
    return {
        "programId": PROGRAM_ID,
        "requestedState": requested,
        "actualState": actual,
        "sessionId": read_text(CONTROL_DIR / "session.id") or None,
        "startedAt": read_text(CONTROL_DIR / "started.at") or None,
        "stoppedAt": read_text(CONTROL_DIR / "stopped.at") or None,
        "publisherConnected": publisher_connected,
        "mode": mode,
        "destinations": destinations,
        "destinationConfiguration": {
            "version": active_destinations.selection.version,
            "selectionHash": active_destinations.selection.selection_hash,
            "count": len(active_destinations.destinations),
        } if active_destinations else None,
        "lastCommand": load_last_command(),
        "filler": FILLER_STORE.public_state(active_filler) if active_filler else None,
        "lastPreparation": load_json(CONTROL_DIR / "last-preparation.json"),
    }


def load_json(path: Path) -> dict[str, object] | None:
    raw = read_text(path)
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def command_record_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return CONTROL_DIR / "commands" / f"{digest}.json"


def destination_record_path(key: str) -> Path:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    return CONTROL_DIR / "destination-commands" / f"{digest}.json"


def record_command(key: str, record: dict[str, object]) -> None:
    atomic_write(command_record_path(key), json.dumps(record, separators=(",", ":"), sort_keys=True))
    atomic_write(CONTROL_DIR / "last-command.json", json.dumps(record, separators=(",", ":"), sort_keys=True))
    atomic_write(CONTROL_DIR / "last.sequence", str(record["sequence"]))


def destination_provider():
    global DESTINATION_PROVIDER
    if DESTINATION_PROVIDER is None:
        DESTINATION_PROVIDER = provider_from_environment()
    return DESTINATION_PROVIDER


def resolve_destinations(selection: DestinationSelection):
    try:
        resolved = resolve_selection(selection, destination_provider())
    except DestinationError as exc:
        outcome = "unavailable" if exc.reason in {"secret_unavailable", "provider_unavailable"} else "invalid"
        DESTINATION_METRICS[outcome] += 1
        raise
    DESTINATION_METRICS["success"] += 1
    return resolved


def apply_command(
    action: str,
    key: str,
    sequence: int,
    filler_version: str | None,
    destination_selection: DestinationSelection | None,
) -> tuple[int, dict[str, object]]:
    prior_path = command_record_path(key)
    if prior_path.exists():
        prior = json.loads(read_text(prior_path))
        selection_hash = destination_selection.selection_hash if destination_selection else None
        if (
            prior.get("action") != action
            or prior.get("fillerVersion") != filler_version
            or prior.get("destinationSelectionHash") != selection_hash
        ):
            state = current_state()
            state["error"] = "idempotency key was reused for another command"
            return 409, state
        state = current_state()
        state["commandResult"] = {**prior, "duplicate": True}
        return 200, state

    last_sequence = int(read_text(CONTROL_DIR / "last.sequence", "0"))
    if sequence <= last_sequence:
        state = current_state()
        state["error"] = "command sequence is not newer than the last accepted command"
        state["lastAcceptedSequence"] = last_sequence
        return 409, state

    current = requested_state()
    if action == "start":
        if destination_selection is None:
            state = current_state()
            state["error"] = "a bounded destination selection is required"
            return 400, state
        try:
            prepared = bool(filler_version and FILLER_STORE.manifest(filler_version))
        except PreparationError:
            prepared = False
        if not prepared:
            state = current_state()
            state["error"] = "requested filler version is not prepared"
            return 409, state
        if current == "started":
            active_destinations = DESTINATION_RUNTIME.active
            if (
                read_text(CONTROL_DIR / "active-filler.version") != filler_version
                or active_destinations is None
                or active_destinations.selection.selection_hash != destination_selection.selection_hash
            ):
                state = current_state()
                state["error"] = "active session is bound to another immutable configuration"
                return 409, state
            result = "already-started"
        else:
            try:
                resolved = resolve_destinations(destination_selection)
                DESTINATION_RUNTIME.activate(resolved)
            except DestinationError as exc:
                state = current_state()
                state["error"] = exc.reason
                return 503 if exc.reason in {"secret_unavailable", "provider_unavailable"} else 422, state
            try:
                session_id = str(uuid.uuid4())
                atomic_write(CONTROL_DIR / "session.id", session_id)
                atomic_write(CONTROL_DIR / "started.at", now())
                (CONTROL_DIR / "stopped.at").unlink(missing_ok=True)
                (HOOK_DIR / "publisher.seen").unlink(missing_ok=True)
                if (HOOK_DIR / "publisher").exists():
                    atomic_write(HOOK_DIR / "publisher.seen", now())
                atomic_write(CONTROL_DIR / "active-filler.version", filler_version)
                # Commit the public lifecycle transition last. Supervisors
                # cannot open destinations while this remains stopped.
                atomic_write(CONTROL_DIR / "requested.state", "started")
            except OSError:
                DESTINATION_RUNTIME.deactivate()
                state = current_state()
                state["error"] = "runtime_activation_failed"
                return 503, state
            result = "started"
    else:
        if current == "stopped":
            result = "already-stopped"
            DESTINATION_RUNTIME.deactivate()
        else:
            atomic_write(CONTROL_DIR / "requested.state", "stopped")
            (HOOK_DIR / "publisher.seen").unlink(missing_ok=True)
            (CONTROL_DIR / "active-filler.version").unlink(missing_ok=True)
            atomic_write(CONTROL_DIR / "stopped.at", now())
            DESTINATION_RUNTIME.deactivate()
            result = "stopped"

    record = {
        "id": key,
        "sequence": sequence,
        "action": action,
        "result": result,
        "acceptedAt": now(),
        "fillerVersion": filler_version if action == "start" else None,
        "destinationVersion": destination_selection.version if destination_selection else None,
        "destinationSelectionHash": destination_selection.selection_hash if destination_selection else None,
        "destinationCount": len(destination_selection.references) if destination_selection else None,
    }
    record_command(key, record)

    if action == "stop" and result == "stopped":
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            state = current_state()
            if state["actualState"] == "stopped":
                break
            time.sleep(0.1)

    state = current_state()
    state["commandResult"] = record
    return 200, state


class Handler(BaseHTTPRequestHandler):
    server_version = "croccante-control"
    sys_version = ""

    def begin_metric(self, route: str) -> None:
        self._metric_route = route
        self._metric_started = time.monotonic()

    def record_metric(self, status: int) -> None:
        if status < 300:
            result = "success"
        elif status == 400:
            result = "invalid"
        elif status == 401:
            result = "unauthorized"
        elif status == 404:
            result = "not_found"
        elif status == 409:
            result = "conflict"
        else:
            result = "error"
        relay_metrics.record_control_request(
            self.command,
            getattr(self, "_metric_route", "unknown"),
            result,
            time.monotonic() - getattr(self, "_metric_started", time.monotonic()),
        )

    def log_message(self, format_string: str, *args: object) -> None:
        # Do not log request headers or bodies. The entrypoint emits only the
        # bind address, program identifier, actions, and response codes.
        status = args[1] if len(args) > 1 else "-"
        print(f"[control] {self.command} {urlsplit(self.path).path} {status}", flush=True)

    def send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.record_metric(status)
        self.send_response(status)
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_metrics(self) -> None:
        try:
            body = (relay_metrics.collect() + self.filler_metrics()).encode("utf-8")
        except Exception:
            self.send_json(500, {"error": "metrics unavailable"})
            return
        self.record_metric(200)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self) -> bool:
        expected = read_text(TOKEN_FILE)
        supplied = self.headers.get("Authorization", "")
        return bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")

    def authorize_and_scope(self) -> bool:
        if not self.authenticated():
            self.send_json(401, {"error": "unauthorized"})
            return False
        path = urlsplit(self.path).path
        prefix = "/v1/programs/"
        if not path.startswith(prefix):
            self.send_json(404, {"error": "not found"})
            return False
        encoded_program = path[len(prefix):].split("/", 1)[0]
        if unquote(encoded_program) != PROGRAM_ID:
            self.send_json(404, {"error": "program not found"})
            return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path == "/metrics":
            self.begin_metric("metrics")
            if not self.authenticated():
                self.send_json(401, {"error": "unauthorized"})
                return
            self.send_metrics()
            return
        route = "filler" if path.startswith(FILLER_PATH) else "session" if path == PROGRAM_PATH else "unknown"
        self.begin_metric(route)
        if not self.authorize_and_scope():
            return
        if path.startswith(FILLER_PATH):
            version = unquote(path[len(FILLER_PATH):])
            try:
                state = FILLER_STORE.public_state(version)
            except PreparationError:
                self.send_json(404, {"error": "not found"})
                return
            self.send_json(200 if state["ready"] else 404, state)
            return
        if path != PROGRAM_PATH:
            self.send_json(404, {"error": "not found"})
            return
        self.send_json(200, current_state())

    def filler_metrics(self) -> str:
        prepared = sum(
            1 for child in FILLER_STORE.program_root.iterdir()
            if child.is_dir() and not child.name.startswith(".") and FILLER_STORE.manifest(child.name)
        )
        active = 1 if read_text(CONTROL_DIR / "active-filler.version") else 0
        lines = [
            "# HELP croccante_filler_preparation_total Filler preparation outcomes.",
            "# TYPE croccante_filler_preparation_total counter",
        ]
        for outcome in ("success", "failure", "conflict"):
            lines.append(f'croccante_filler_preparation_total{{outcome="{outcome}"}} {METRICS[outcome]}')
        lines += [
            "# HELP croccante_filler_prepared_versions Prepared version inventory.",
            "# TYPE croccante_filler_prepared_versions gauge",
            f"croccante_filler_prepared_versions {prepared}",
            "# HELP croccante_filler_active_version Whether a session has a bound filler.",
            "# TYPE croccante_filler_active_version gauge",
            f"croccante_filler_active_version {active}",
            "# HELP croccante_destination_resolution_total Destination configuration resolution outcomes.",
            "# TYPE croccante_destination_resolution_total counter",
        ]
        for outcome in ("success", "invalid", "unavailable", "conflict"):
            lines.append(f'croccante_destination_resolution_total{{outcome="{outcome}"}} {DESTINATION_METRICS[outcome]}')
        return "\n".join(lines) + "\n"

    def do_PUT(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        route = "destinations" if path.startswith(DESTINATION_PATH) else "filler"
        self.begin_metric(route)
        if not self.authorize_and_scope():
            return
        if path.startswith(DESTINATION_PATH):
            version = unquote(path[len(DESTINATION_PATH):])
            if not version or "/" in version:
                self.send_json(404, {"error": "not found"})
                return
            key = self.headers.get("Idempotency-Key", "").strip()
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if not key or len(key) > 200 or length < 2 or length > 65536:
                self.send_json(400, {"error": "bounded body and Idempotency-Key are required"})
                return
            try:
                request = json.loads(self.rfile.read(length))
                if not isinstance(request, dict) or set(request) != {"commandId", "version", "destinations"} or request.get("commandId") != key:
                    raise ValueError
                selection = parse_selection(
                    {"version": request["version"], "destinations": request["destinations"]},
                    expected_version=version,
                )
            except (json.JSONDecodeError, ValueError, DestinationError) as exc:
                if isinstance(exc, DestinationError):
                    DESTINATION_METRICS["invalid"] += 1
                self.send_json(400, {"error": "invalid destination configuration"})
                return
            with COMMAND_LOCK:
                if requested_state() != "stopped":
                    DESTINATION_METRICS["conflict"] += 1
                    self.send_json(409, {"error": "destination reconfiguration requires a stopped session"})
                    return
                record_path = destination_record_path(key)
                if record_path.exists():
                    prior = load_json(record_path)
                    if prior is None or prior.get("selectionHash") != selection.selection_hash:
                        DESTINATION_METRICS["conflict"] += 1
                        self.send_json(409, {"error": "idempotency key was reused for another configuration"})
                        return
                    self.send_json(200, {**prior, "duplicate": True})
                    return
                try:
                    resolved = resolve_destinations(selection)
                except DestinationError as exc:
                    self.send_json(503 if exc.reason in {"secret_unavailable", "provider_unavailable"} else 422, {"error": exc.reason})
                    return
                record = {
                    "id": key,
                    "version": selection.version,
                    "selectionHash": selection.selection_hash,
                    "destinationCount": len(resolved.destinations),
                    "result": "validated",
                    "acceptedAt": now(),
                }
                atomic_write(record_path, canonical_json(record))
                self.send_json(200, record)
            return
        if not path.startswith(FILLER_PATH):
            self.send_json(404, {"error": "not found"})
            return
        version = unquote(path[len(FILLER_PATH):])
        if not version or "/" in version:
            self.send_json(404, {"error": "not found"})
            return
        key = self.headers.get("Idempotency-Key", "").strip()
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if not key or len(key) > 200 or length < 2 or length > 65536:
            self.send_json(400, {"error": "bounded body and Idempotency-Key are required"})
            return
        try:
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or request.get("commandId") != key:
                raise ValueError
        except (json.JSONDecodeError, ValueError):
            self.send_json(400, {"error": "invalid preparation request"})
            return
        atomic_write(CONTROL_DIR / "last-preparation.json", canonical_json({
            "version": version, "status": "preparing", "ready": False,
            "acceptedAt": now(),
        }))
        try:
            with PREPARATION_LOCK:
                result = FILLER_STORE.prepare(version, request, now())
                FILLER_STORE.cleanup(read_text(CONTROL_DIR / "active-filler.version") or None)
        except PreparationError as exc:
            outcome = "conflict" if exc.reason == "version-conflict" else "failure"
            METRICS[outcome] += 1
            failure = {"version": version, "status": "failed", "ready": False, "reason": exc.reason, "failedAt": now()}
            atomic_write(CONTROL_DIR / "last-preparation.json", canonical_json(failure))
            self.send_json(409 if outcome == "conflict" else 422, failure)
            return
        METRICS["success"] += 1
        atomic_write(CONTROL_DIR / "last-preparation.json", canonical_json(result))
        self.send_json(200, result)

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        start_path = f"{PROGRAM_PATH}/start"
        stop_path = f"{PROGRAM_PATH}/stop"
        route = "start" if path == start_path else "stop" if path == stop_path else "unknown"
        self.begin_metric(route)
        if not self.authorize_and_scope():
            return
        if path not in (start_path, stop_path):
            self.send_json(404, {"error": "not found"})
            return
        key = self.headers.get("Idempotency-Key", "").strip()
        sequence_text = self.headers.get("X-Command-Sequence", "").strip()
        if not key or len(key) > 200:
            self.send_json(400, {"error": "a bounded Idempotency-Key is required"})
            return
        try:
            sequence = int(sequence_text)
            if sequence < 1:
                raise ValueError
        except ValueError:
            self.send_json(400, {"error": "X-Command-Sequence must be a positive integer"})
            return
        action = path.rsplit("/", 1)[1]
        filler_version = self.headers.get("X-Filler-Version", "").strip() or None
        destination_selection = None
        if action == "start":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length < 2 or length > 65536:
                self.send_json(400, {"error": "a bounded destination selection is required"})
                return
            try:
                destination_selection = parse_selection(json.loads(self.rfile.read(length)))
            except (json.JSONDecodeError, DestinationError):
                DESTINATION_METRICS["invalid"] += 1
                self.send_json(400, {"error": "invalid destination selection"})
                return
        with COMMAND_LOCK:
            status, payload = apply_command(action, key, sequence, filler_version, destination_selection)
        self.send_json(status, payload)


def main() -> None:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    (CONTROL_DIR / "commands").mkdir(exist_ok=True)
    (CONTROL_DIR / "destination-commands").mkdir(exist_ok=True)
    if not (CONTROL_DIR / "requested.state").exists():
        atomic_write(CONTROL_DIR / "requested.state", "stopped")
    def stop_runtime(*_args: object) -> None:
        DESTINATION_RUNTIME.deactivate()
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, stop_runtime)
    signal.signal(signal.SIGINT, stop_runtime)
    server = ThreadingHTTPServer((CONTROL_BIND, CONTROL_PORT), Handler)
    print(f"[control] listening on {CONTROL_BIND}:{CONTROL_PORT} for program={PROGRAM_ID}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
