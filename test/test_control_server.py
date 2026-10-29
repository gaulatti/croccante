#!/usr/bin/env python3
"""In-process tests for the private lifecycle API (control-server.py).

The real HTTP handler, command journal, state machine, filler store and
destination runtime run in this process. Only the outside world is replaced:
the destination supervisor is a tiny shell script (or a recording fake), secrets
come from the explicit non-production file provider, ffmpeg and the network are
faked where filler preparation would reach them, and process liveness, time and
signal registration are fault-injected where a test needs a specific outcome.
No Docker, nginx-rtmp or ffmpeg process is required.
"""

from __future__ import annotations

import contextlib
import hashlib
import http.client
import importlib.util
import io
import json
import os
import runpy
import signal
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PROGRAM_ID", "bounded-test-program")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import destination_runtime
import relay_metrics
from destination_store import DestinationError
from filler_store import FillerStore, PreparationError
from test_metrics import parse_exposition

SERVER_PATH = Path(__file__).resolve().parents[1] / "control-server.py"
PROGRAM = "program-a"
TOKEN = "private-control-token"
SELECTION = {
    "version": "destinations-v1",
    "destinations": [{"id": "sink-a", "secretId": "secret-a", "versionId": "version-a"}],
}
SECRETS = {
    "secret-a": {
        "version-a": {"scheme": "rtmp", "host": "ingest.example", "application": "live", "streamKey": "fictional-key-a"},
        "bad-payload": {"scheme": "gopher", "host": "ingest.example", "application": "live", "streamKey": "fictional-key-c"},
    },
    "secret-b": {
        "version-b": {"scheme": "rtmps", "host": "other.example", "port": 443, "application": "app", "streamKey": "fictional-key-b"},
    },
}
SUPERVISOR = "#!/bin/sh\nexec sleep 300\n"
PROFILE = {
    "width": 160, "height": 90, "fps": 10, "videoBitrate": "200k",
    "audioRate": 44100, "audioChannels": 2, "audioBitrate": "64k", "gop": 10, "loopSeconds": 2,
}
SOURCE_BYTES = b"fictional-source"
OUTPUT_BYTES = b"fictional-flv"
OUTPUT_PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 160, "height": 90, "avg_frame_rate": "10/1", "pix_fmt": "yuv420p"},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2},
    ]
}


def load_server(environment: dict[str, str]):
    with patch.dict(os.environ, environment, clear=False):
        spec = importlib.util.spec_from_file_location("control_server_under_test", SERVER_PATH)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
    return module


class RecordingRuntime:
    """Fake destination runtime that records lifecycle calls."""

    def __init__(self, failure: DestinationError | None = None) -> None:
        self.active = None
        self.failure = failure
        self.calls: list[str] = []

    def activate(self, plan: object) -> None:
        self.calls.append("activate")
        if self.failure is not None:
            raise self.failure
        self.active = plan

    def deactivate(self) -> None:
        self.calls.append("deactivate")
        self.active = None


class ControlServerCase(unittest.TestCase):
    start_server = True

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state"
        self.state.mkdir()
        (self.state / "hooks").mkdir()
        self.token = self.root / "token"
        self.token.write_text(f"{TOKEN}\n", encoding="utf-8")
        self.secrets = self.root / "secrets.json"
        self.secrets.write_text(json.dumps(SECRETS), encoding="utf-8")
        self.supervisor = self.root / "supervisor.sh"
        self.supervisor.write_text(SUPERVISOR, encoding="utf-8")
        self.supervisor.chmod(0o700)
        self.output = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.output))
        stack.enter_context(patch.object(relay_metrics, "STATE_DIR", self.state))
        # Skip only the runtime's fixed half-second exec-settle pause.
        stack.enter_context(patch.object(
            destination_runtime, "time", types.SimpleNamespace(sleep=lambda _seconds: None, monotonic=time.monotonic)
        ))
        relay_metrics._control_counts.clear()
        relay_metrics._control_duration_sums.clear()
        environment = {
            "PROGRAM_ID": PROGRAM,
            "STATE_DIR": str(self.state),
            "FILLER_STORE_DIR": str(self.root / "fillers"),
            "CONTROL_TOKEN_FILE": str(self.token),
            "CONTROL_BIND": "127.0.0.1",
            "CONTROL_PORT": "0",
            "RELAY_SUPERVISOR": str(self.supervisor),
            "DESTINATION_SECRET_PROVIDER": "file",
            "CROCCANTE_ENVIRONMENT": "test",
            "DESTINATION_FAKE_SECRETS_FILE": str(self.secrets),
        }
        # Secret-provider selection is read lazily at the first Start, so the
        # explicit non-production configuration stays in place for the test.
        stack.enter_context(patch.dict(os.environ, environment, clear=False))
        self.cs = load_server(environment)
        self.cs.CONTROL_DIR.mkdir(parents=True, exist_ok=True)
        (self.cs.CONTROL_DIR / "commands").mkdir(exist_ok=True)
        (self.cs.CONTROL_DIR / "destination-commands").mkdir(exist_ok=True)
        self.server = None
        if self.start_server:
            self.server = self.cs.ThreadingHTTPServer(("127.0.0.1", 0), self.cs.Handler)
            self.port = self.server.server_port
            self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            self.thread.start()
        self.addCleanup(self.shutdown)

    def shutdown(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
        self.cs.DESTINATION_RUNTIME.deactivate()
        self.temporary.cleanup()

    # helpers ---------------------------------------------------------------
    def call(self, method: str, path: str, *, body: object | None = None, raw: bytes | None = None,
             headers: dict[str, str] | None = None, token: str | None = TOKEN) -> tuple[int, dict, http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        request_headers = dict(headers or {})
        if token is not None:
            request_headers["Authorization"] = f"Bearer {token}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            connection.request(method, path, body=data, headers=request_headers)
            response = connection.getresponse()
            payload = response.read()
        finally:
            connection.close()
        return response.status, (json.loads(payload) if payload.startswith(b"{") else {"text": payload.decode()}), response

    def session_url(self, suffix: str = "") -> str:
        return f"/v1/programs/{PROGRAM}/session{suffix}"

    def prepare_version(self, version: str = "v1") -> None:
        directory = self.cs.FILLER_STORE.program_root / version
        directory.mkdir()
        (directory / "filler.flv").write_bytes(OUTPUT_BYTES)
        (directory / "manifest.json").write_text(json.dumps({
            "version": version, "status": "ready", "ready": True, "sourceId": "source-a",
            "sourceSha256": "a" * 64, "artifactSha256": hashlib.sha256(OUTPUT_BYTES).hexdigest(),
            "profile": PROFILE, "preparedAt": "2026-10-28T00:00:00Z", "requestHash": "b" * 64,
        }), encoding="utf-8")

    def start(self, key: str = "start-1", sequence: int = 1, version: str | None = "v1",
              selection: object = SELECTION) -> tuple[int, dict]:
        headers = {"Idempotency-Key": key, "X-Command-Sequence": str(sequence)}
        if version is not None:
            headers["X-Filler-Version"] = version
        status, payload, _ = self.call("POST", self.session_url("/start"), body=selection, headers=headers)
        return status, payload

    def stop(self, key: str = "stop-1", sequence: int = 2) -> tuple[int, dict]:
        status, payload, _ = self.call(
            "POST", self.session_url("/stop"), headers={"Idempotency-Key": key, "X-Command-Sequence": str(sequence)}
        )
        return status, payload

    def write_state(self, name: str, value: str) -> None:
        (self.state / name).write_text(f"{value}\n", encoding="utf-8")

    def write_destinations(self, *modes: str, wrapper_pid: str | None = None, ffmpeg_pid: str | None = None) -> None:
        self.write_state("dest.count", str(len(modes)))
        for index, mode in enumerate(modes, 1):
            self.write_state(f"dest-{index}.state", mode)
            self.write_state(f"dest-{index}.id", f"slot-{index}")
            self.write_state(f"dest-{index}.wrapper.pid", wrapper_pid or str(os.getpid()))
            if ffmpeg_pid:
                self.write_state(f"dest-{index}.ffmpeg.pid", ffmpeg_pid)

    def request_with_runtime(self, runtime: RecordingRuntime) -> None:
        self.cs.DESTINATION_RUNTIME = runtime


class StateFileHelpersTest(ControlServerCase):
    start_server = False

    def test_read_text_tolerates_missing_and_unreadable_paths(self) -> None:
        self.assertEqual(self.cs.read_text(self.state / "absent", "fallback"), "fallback")
        self.assertEqual(self.cs.read_text(self.state, "directory"), "directory")
        self.write_state("value", "  stored  ")
        self.assertEqual(self.cs.read_text(self.state / "value"), "stored")

    def test_atomic_write_replaces_without_leaving_temporaries(self) -> None:
        target = self.state / "atomic"
        self.cs.atomic_write(target, "one")
        self.cs.atomic_write(target, "two")
        self.assertEqual(target.read_text(), "two\n")
        self.assertEqual([item.name for item in self.state.iterdir() if item.name.endswith(".tmp")], [])

    def test_pid_liveness_distinguishes_every_outcome(self) -> None:
        self.write_state("pid", "777")
        for failure in (ProcessLookupError(), PermissionError(), OSError("other")):
            with self.subTest(failure=type(failure).__name__), patch.object(self.cs.os, "kill", side_effect=failure):
                self.assertFalse(self.cs.pid_alive(self.state / "pid"))
        with patch.object(self.cs.os, "kill") as kill:
            self.assertTrue(self.cs.pid_alive(self.state / "pid"))
        kill.assert_called_once_with(777, 0)
        self.assertFalse(self.cs.pid_alive(self.state / "absent"))
        self.write_state("pid", "not-a-pid")
        self.assertFalse(self.cs.pid_alive(self.state / "pid"))

    def test_json_documents_are_parsed_defensively(self) -> None:
        path = self.state / "document.json"
        self.assertIsNone(self.cs.load_json(path))
        path.write_text("{", encoding="utf-8")
        self.assertIsNone(self.cs.load_json(path))
        path.write_text("[1]", encoding="utf-8")
        self.assertIsNone(self.cs.load_json(path))
        path.write_text('{"ok": true}', encoding="utf-8")
        self.assertEqual(self.cs.load_json(path), {"ok": True})

    def test_last_command_reports_corruption_without_raising(self) -> None:
        self.assertIsNone(self.cs.load_last_command())
        (self.cs.CONTROL_DIR / "last-command.json").write_text("{", encoding="utf-8")
        self.assertEqual(self.cs.load_last_command(), {"result": "invalid-state"})
        (self.cs.CONTROL_DIR / "last-command.json").write_text('{"result": "started"}', encoding="utf-8")
        self.assertEqual(self.cs.load_last_command(), {"result": "started"})

    def test_idempotency_records_are_addressed_by_key_digest_only(self) -> None:
        digest = hashlib.sha256(b"key-1").hexdigest()
        self.assertEqual(self.cs.command_record_path("key-1"), self.cs.CONTROL_DIR / "commands" / f"{digest}.json")
        self.assertEqual(self.cs.destination_record_path("key-1"), self.cs.CONTROL_DIR / "destination-commands" / f"{digest}.json")

    def test_destination_inventory_ignores_a_corrupt_count(self) -> None:
        self.write_state("dest.count", "many")
        self.assertEqual(self.cs.destination_state(), [])
        self.write_destinations("relaying", "idle", ffmpeg_pid=str(os.getpid()))
        inventory = self.cs.destination_state()
        self.assertEqual([item["id"] for item in inventory], ["slot-1", "slot-2"])
        self.assertTrue(all(item["supervisorHealthy"] and item["publisherProcessHealthy"] for item in inventory))
        (self.state / "dest-1.id").unlink()
        self.assertIsNone(self.cs.destination_state()[0]["id"])


class StateMachineTest(ControlServerCase):
    start_server = False

    def observe(self, requested: str, *modes: str, connected: bool = False, seen: bool = False,
                wrapper_pid: str | None = None, ffmpeg_pid: str | None = None) -> dict:
        (self.cs.CONTROL_DIR / "requested.state").write_text(f"{requested}\n", encoding="utf-8")
        if modes:
            self.write_destinations(*modes, wrapper_pid=wrapper_pid, ffmpeg_pid=ffmpeg_pid)
        for name, present in (("publisher", connected), ("publisher.seen", seen)):
            marker = self.state / "hooks" / name
            if present:
                marker.write_text("1\n", encoding="utf-8")
        return self.cs.current_state()

    def test_stopped_sessions_are_idle_until_every_output_is_quiet(self) -> None:
        self.assertEqual(self.cs.requested_state(), "stopped")
        quiet = self.observe("stopped")
        self.assertEqual((quiet["mode"], quiet["actualState"]), ("idle", "stopped"))
        busy = self.observe("stopped", "relaying")
        self.assertEqual((busy["mode"], busy["actualState"]), ("stopping", "stopping"))

    def test_stopped_session_with_a_live_publisher_process_is_still_stopping(self) -> None:
        state = self.observe("stopped", "idle", ffmpeg_pid=str(os.getpid()))
        self.assertEqual(state["actualState"], "stopping")

    def test_started_sessions_fail_without_healthy_supervisors(self) -> None:
        self.assertEqual(self.observe("started")["actualState"], "failed")
        with patch.object(self.cs.os, "kill", side_effect=ProcessLookupError):
            dead = self.observe("started", "relaying", wrapper_pid="424242")
        self.assertEqual((dead["mode"], dead["actualState"]), ("failed", "failed"))

    def test_connected_publisher_modes(self) -> None:
        cases = {
            ("relaying", "idle"): ("live", "started"),
            ("backoff", "backoff"): ("failed", "failed"),
            ("waiting", "backoff"): ("waiting-for-publisher", "started"),
        }
        for modes, expected in cases.items():
            with self.subTest(modes=modes):
                state = self.observe("started", *modes, connected=True)
                self.assertEqual((state["mode"], state["actualState"]), expected)
                self.assertTrue(state["publisherConnected"])

    def test_gap_modes_after_the_publisher_was_seen(self) -> None:
        cases = {
            ("filler",): ("filler", "started"),
            ("backoff",): ("failed", "failed"),
            ("waiting",): ("waiting-for-publisher", "started"),
        }
        for modes, expected in cases.items():
            with self.subTest(modes=modes):
                state = self.observe("started", *modes, seen=True)
                self.assertEqual((state["mode"], state["actualState"]), expected)
                self.assertFalse(state["publisherConnected"])

    def test_started_without_any_publisher_waits(self) -> None:
        state = self.observe("started", "waiting")
        self.assertEqual((state["mode"], state["actualState"]), ("waiting-for-publisher", "started"))

    def test_public_state_reports_filler_preparation_and_configuration(self) -> None:
        self.prepare_version("v1")
        self.write_state("control/active-filler.version", "v1")
        (self.cs.CONTROL_DIR / "last-preparation.json").write_text(json.dumps({"version": "v1", "status": "ready"}), encoding="utf-8")
        state = self.observe("started", "waiting")
        self.assertEqual(state["filler"]["version"], "v1")
        self.assertEqual(state["lastPreparation"], {"version": "v1", "status": "ready"})
        self.assertIsNone(state["destinationConfiguration"])
        self.assertEqual(state["programId"], PROGRAM)
        self.assertNotIn("fictional-key", json.dumps(state))


class AuthenticationAndRoutingTest(ControlServerCase):
    def test_credentials_are_required_on_every_route(self) -> None:
        for method, path in (
            ("GET", "/metrics"), ("GET", self.session_url()), ("PUT", f"/v1/programs/{PROGRAM}/fillers/v1"),
            ("PUT", f"/v1/programs/{PROGRAM}/destinations/v1"), ("POST", self.session_url("/start")),
        ):
            for supplied in (None, "wrong-token"):
                with self.subTest(method=method, path=path, token=supplied):
                    status, payload, response = self.call(method, path, token=supplied)
                    self.assertEqual(status, 401)
                    self.assertEqual(payload, {"error": "unauthorized"})
                    self.assertEqual(response.getheader("WWW-Authenticate"), "Bearer")

    def test_missing_token_file_never_authenticates(self) -> None:
        self.token.unlink()
        self.assertEqual(self.call("GET", self.session_url(), token="")[0], 401)
        self.assertEqual(self.call("GET", self.session_url())[0], 401)

    def test_unknown_routes_and_other_programs_are_not_found(self) -> None:
        for method in ("GET", "PUT", "POST"):
            with self.subTest(method=method):
                self.assertEqual(self.call(method, "/other")[0], 404)
                self.assertEqual(self.call(method, "/v1/programs/someone-else/session")[0], 404)
        self.assertEqual(self.call("GET", f"/v1/programs/{PROGRAM}/unknown")[0], 404)
        self.assertEqual(self.call("PUT", f"/v1/programs/{PROGRAM}/session")[0], 404)
        self.assertEqual(self.call("POST", self.session_url("/other"))[0], 404)

    def test_session_snapshot_is_served_and_never_cached(self) -> None:
        status, payload, response = self.call("GET", self.session_url())
        self.assertEqual(status, 200)
        self.assertEqual(payload["requestedState"], "stopped")
        self.assertEqual(response.getheader("Cache-Control"), "no-store")
        self.assertEqual(response.getheader("Content-Type"), "application/json")

    def test_request_log_omits_credentials_and_query_strings(self) -> None:
        self.call("GET", self.session_url() + "?signature=private-query")
        text = self.output.getvalue()
        self.assertIn(f"[control] GET /v1/programs/{PROGRAM}/session 200", text)
        self.assertNotIn(TOKEN, text)
        self.assertNotIn("private-query", text)
        stub = types.SimpleNamespace(command="GET", path="/v1/x?token=abc")
        self.cs.Handler.log_message(stub, "%s", "only-one-argument")
        self.assertIn("[control] GET /v1/x -", self.output.getvalue())


class MetricsEndpointTest(ControlServerCase):
    def test_metrics_include_filler_inventory_and_destination_outcomes(self) -> None:
        self.prepare_version("v1")
        (self.cs.FILLER_STORE.program_root / ".hidden").mkdir()
        (self.cs.FILLER_STORE.program_root / "unprepared").mkdir()
        (self.cs.FILLER_STORE.program_root / "stray-file").write_text("x", encoding="utf-8")
        self.write_state("control/active-filler.version", "v1")
        status, payload, response = self.call("GET", "/metrics")
        self.assertEqual(status, 200)
        samples = parse_exposition(payload["text"])
        values = {(name, tuple(sorted(labels.items()))): value for name, labels, value in samples}
        self.assertEqual(values[("croccante_filler_prepared_versions", ())], 1.0)
        self.assertEqual(values[("croccante_filler_active_version", ())], 1.0)
        for outcome in ("success", "failure", "conflict"):
            self.assertIn(("croccante_filler_preparation_total", (("outcome", outcome),)), values)
        for outcome in ("success", "invalid", "unavailable", "conflict"):
            self.assertIn(("croccante_destination_resolution_total", (("outcome", outcome),)), values)
        self.assertTrue(response.getheader("Content-Type").startswith("text/plain"))
        self.assertNotIn(TOKEN, payload["text"])

    def test_inactive_filler_reports_zero(self) -> None:
        samples = parse_exposition(self.call("GET", "/metrics")[1]["text"])
        self.assertIn(("croccante_filler_active_version", {}, 0.0), samples)

    def test_collector_failure_is_a_bounded_server_error(self) -> None:
        with patch.object(relay_metrics, "collect", side_effect=RuntimeError("private detail")):
            status, payload, _ = self.call("GET", "/metrics")
        self.assertEqual((status, payload), (500, {"error": "metrics unavailable"}))
        samples = parse_exposition(self.call("GET", "/metrics")[1]["text"])
        results = {labels["result"] for name, labels, _ in samples if name == "croccante_control_requests_total"}
        self.assertIn("error", results)

    def test_every_status_class_is_counted_with_bounded_labels(self) -> None:
        self.call("GET", self.session_url())
        self.call("GET", self.session_url(), token="bad")
        self.call("GET", "/nowhere")
        self.start(selection="not-an-object")
        samples = parse_exposition(self.call("GET", "/metrics")[1]["text"])
        results = {labels["result"] for name, labels, _ in samples if name == "croccante_control_requests_total"}
        self.assertTrue({"success", "unauthorized", "not_found", "invalid"} <= results)
        self.stop(sequence=1)
        self.stop(key="stop-2", sequence=1)
        samples = parse_exposition(self.call("GET", "/metrics")[1]["text"])
        results = {labels["result"] for name, labels, _ in samples if name == "croccante_control_requests_total"}
        self.assertIn("conflict", results)


class FillerRoutesTest(ControlServerCase):
    def filler_request(self, command: str = "prepare-1") -> dict[str, object]:
        return {
            "commandId": command,
            "source": {"id": "source-a", "sha256": hashlib.sha256(SOURCE_BYTES).hexdigest(), "downloadUrl": "https://media.example/private?signature=secret"},
            "profile": PROFILE,
        }

    def put_filler(self, version: str, body: object | None = None, **kwargs) -> tuple[int, dict]:
        headers = kwargs.pop("headers", {"Idempotency-Key": "prepare-1"})
        status, payload, _ = self.call("PUT", f"/v1/programs/{PROGRAM}/fillers/{version}", body=body, headers=headers, **kwargs)
        return status, payload

    @contextlib.contextmanager
    def fake_media(self):
        def download(url: str, destination: Path) -> None:
            destination.write_bytes(SOURCE_BYTES)

        def transcode(source: Path, output: Path, profile: object, probe: dict) -> None:
            output.write_bytes(OUTPUT_BYTES)

        probes = iter([{"streams": [{"codec_type": "video", "codec_name": "h264"}]}, OUTPUT_PROBE, {"streams": [{"codec_type": "video", "codec_name": "h264"}]}, OUTPUT_PROBE])
        with (
            patch.object(FillerStore, "_download", staticmethod(download)),
            patch.object(FillerStore, "_probe", staticmethod(lambda path: next(probes))),
            patch.object(FillerStore, "_transcode", staticmethod(transcode)),
        ):
            yield

    def test_preparation_publishes_redacted_immutable_state(self) -> None:
        with self.fake_media():
            status, payload = self.put_filler("v1", self.filler_request())
        self.assertEqual(status, 200, payload)
        self.assertTrue(payload["ready"])
        self.assertNotIn("downloadUrl", json.dumps(payload))
        last = json.loads((self.cs.CONTROL_DIR / "last-preparation.json").read_text())
        self.assertEqual(last["status"], "ready")
        status, state, _ = self.call("GET", f"/v1/programs/{PROGRAM}/fillers/v1")
        self.assertEqual((status, state["ready"]), (200, True))
        self.assertEqual(self.cs.METRICS["success"], 1)

    def test_conflicting_and_failing_preparations_have_distinct_outcomes(self) -> None:
        with self.fake_media():
            self.assertEqual(self.put_filler("v1", self.filler_request())[0], 200)
            conflicting = self.filler_request("another-command")
            status, payload = self.put_filler("v1", conflicting, headers={"Idempotency-Key": "another-command"})
        self.assertEqual((status, payload["reason"]), (409, "version-conflict"))
        self.assertEqual(self.cs.METRICS["conflict"], 1)
        bad = self.filler_request("bad")
        bad["source"]["sha256"] = "z"  # type: ignore[index]
        status, payload = self.put_filler("v2", bad, headers={"Idempotency-Key": "bad"})
        self.assertEqual((status, payload["reason"]), (422, "invalid-source"))
        self.assertEqual(self.cs.METRICS["failure"], 1)
        last = json.loads((self.cs.CONTROL_DIR / "last-preparation.json").read_text())
        self.assertEqual((last["status"], last["reason"]), ("failed", "invalid-source"))

    def test_malformed_requests_are_rejected_before_any_work(self) -> None:
        body = self.filler_request()
        cases = {
            "missing-key": ("v1", {"headers": {}}, body),
            "oversized-key": ("v1", {"headers": {"Idempotency-Key": "k" * 201}}, body),
            "empty-body": ("v1", {"headers": {"Idempotency-Key": "prepare-1", "Content-Length": "abc"}}, None),
            "tiny-body": ("v1", {"headers": {"Idempotency-Key": "prepare-1"}, "raw": b"1"}, None),
            "huge-body": ("v1", {"headers": {"Idempotency-Key": "prepare-1", "Content-Length": "70000"}}, None),
        }
        for name, (version, options, payload) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.put_filler(version, payload, **options)[0], 400)
        for name, raw in (("not-json", b"{not json"), ("not-an-object", b"[1, 2]"), ("wrong-command", b'{"commandId": "other"}')):
            with self.subTest(case=name):
                status, payload = self.put_filler("v1", raw=raw)
                self.assertEqual((status, payload), (400, {"error": "invalid preparation request"}))
        self.assertFalse((self.cs.CONTROL_DIR / "last-preparation.json").exists())

    def test_version_segments_must_be_a_single_nonempty_component(self) -> None:
        self.assertEqual(self.put_filler("", self.filler_request())[0], 404)
        self.assertEqual(self.put_filler("a/b", self.filler_request())[0], 404)

    def test_state_queries_for_unprepared_or_invalid_versions_are_not_found(self) -> None:
        status, payload, _ = self.call("GET", f"/v1/programs/{PROGRAM}/fillers/unprepared")
        self.assertEqual((status, payload["ready"]), (404, False))
        self.assertEqual(self.call("GET", f"/v1/programs/{PROGRAM}/fillers/")[0], 404)
        self.assertEqual(self.call("GET", f"/v1/programs/{PROGRAM}/fillers/..%2Fescape")[0], 404)


class DestinationRoutesTest(ControlServerCase):
    def put_destinations(self, version: str = "destinations-v1", body: object | None = None, key: str = "validate-1", **kwargs):
        headers = kwargs.pop("headers", {"Idempotency-Key": key})
        status, payload, _ = self.call("PUT", f"/v1/programs/{PROGRAM}/destinations/{version}", body=body, headers=headers, **kwargs)
        return status, payload

    def configuration(self, command: str = "validate-1", **changes: object) -> dict[str, object]:
        return {"commandId": command, **SELECTION, **changes}

    def test_validation_records_only_non_secret_evidence_and_is_idempotent(self) -> None:
        status, payload = self.put_destinations(body=self.configuration())
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["result"], "validated")
        self.assertEqual(payload["destinationCount"], 1)
        self.assertNotIn("fictional-key", json.dumps(payload))
        replay_status, replay = self.put_destinations(body=self.configuration())
        self.assertEqual(replay_status, 200)
        self.assertTrue(replay["duplicate"])
        self.assertEqual(self.cs.DESTINATION_METRICS["success"], 1)

    def test_reused_keys_and_corrupt_records_conflict(self) -> None:
        self.assertEqual(self.put_destinations(body=self.configuration())[0], 200)
        other = {"version": "destinations-v1", "destinations": [{"id": "sink-a", "secretId": "secret-b", "versionId": "version-b"}]}
        status, payload = self.put_destinations(body={"commandId": "validate-1", **other})
        self.assertEqual((status, payload["error"]), (409, "idempotency key was reused for another configuration"))
        self.cs.destination_record_path("validate-1").write_text("{", encoding="utf-8")
        self.assertEqual(self.put_destinations(body=self.configuration())[0], 409)
        self.assertEqual(self.cs.DESTINATION_METRICS["conflict"], 2)

    def test_resolution_failures_distinguish_unavailable_from_invalid(self) -> None:
        missing = {"version": "destinations-v1", "destinations": [{"id": "sink-a", "secretId": "absent", "versionId": "version-a"}]}
        self.assertEqual(self.put_destinations(body={"commandId": "validate-1", **missing})[0], 503)
        invalid = {"version": "destinations-v1", "destinations": [{"id": "sink-a", "secretId": "secret-a", "versionId": "bad-payload"}]}
        status, payload = self.put_destinations(body={"commandId": "validate-2", **invalid}, key="validate-2")
        self.assertEqual((status, payload), (422, {"error": "unsupported_scheme"}))
        self.assertEqual(self.cs.DESTINATION_METRICS["unavailable"], 1)
        self.assertEqual(self.cs.DESTINATION_METRICS["invalid"], 1)

    def test_reconfiguration_requires_a_stopped_session(self) -> None:
        self.prepare_version("v1")
        self.assertEqual(self.start()[0], 200)
        status, payload = self.put_destinations(body=self.configuration())
        self.assertEqual((status, payload["error"]), (409, "destination reconfiguration requires a stopped session"))

    def test_malformed_requests_are_rejected(self) -> None:
        for name, kwargs in {
            "missing-key": {"headers": {}, "body": self.configuration()},
            "oversized-key": {"headers": {"Idempotency-Key": "k" * 201}, "body": self.configuration()},
            "bad-length": {"headers": {"Idempotency-Key": "validate-1", "Content-Length": "abc"}},
            "tiny": {"raw": b"1"},
            "huge": {"headers": {"Idempotency-Key": "validate-1", "Content-Length": "70000"}},
            "not-json": {"raw": b"{nope"},
            "not-an-object": {"raw": b"[1, 2]"},
            "extra-field": {"body": {**self.configuration(), "streamUrl": "rtmp://forbidden"}},
            "command-mismatch": {"body": self.configuration("someone-else")},
            "version-mismatch": {"body": self.configuration(version="destinations-v2")},
            "invalid-selection": {"body": {"commandId": "validate-1", "version": "destinations-v1", "destinations": []}},
        }.items():
            with self.subTest(case=name):
                status, payload = self.put_destinations(**kwargs)
                self.assertEqual((status, payload), (400, {"error": "invalid destination configuration" if name not in {"missing-key", "oversized-key", "bad-length", "tiny", "huge"} else "bounded body and Idempotency-Key are required"}))
        self.assertEqual(self.cs.DESTINATION_METRICS["invalid"], 2)

    def test_version_segments_must_be_a_single_nonempty_component(self) -> None:
        self.assertEqual(self.put_destinations("", body=self.configuration())[0], 404)
        self.assertEqual(self.put_destinations("a/b", body=self.configuration())[0], 404)


class StartStopTest(ControlServerCase):
    def test_start_binds_one_immutable_session_and_stop_releases_it(self) -> None:
        self.prepare_version("v1")
        status, payload = self.start()
        self.assertEqual(status, 200, payload)
        self.assertEqual(payload["commandResult"]["result"], "started")
        self.assertEqual(payload["requestedState"], "started")
        self.assertEqual(payload["filler"]["version"], "v1")
        self.assertEqual(payload["destinationConfiguration"]["count"], 1)
        self.assertEqual([item["id"] for item in payload["destinations"]], ["sink-a"])
        self.assertTrue(payload["destinations"][0]["supervisorHealthy"])
        self.assertTrue(UUID_PATTERN.fullmatch(payload["sessionId"]))
        self.assertNotIn("fictional-key", json.dumps(payload))
        self.assertEqual((self.state / "dest-1.url").read_text().strip(), "rtmp://ingest.example/live/fictional-key-a")

        stopped_status, stopped = self.stop()
        self.assertEqual(stopped_status, 200, stopped)
        self.assertEqual(stopped["commandResult"]["result"], "stopped")
        self.assertEqual((stopped["requestedState"], stopped["actualState"]), ("stopped", "stopped"))
        self.assertIsNone(stopped["filler"])
        self.assertIsNotNone(stopped["stoppedAt"])
        self.assertEqual(list(self.state.glob("dest-*.url")), [])

    def test_start_records_a_publisher_that_connected_while_stopped(self) -> None:
        self.prepare_version("v1")
        (self.state / "hooks" / "publisher").write_text("1\n")
        (self.state / "hooks" / "publisher.seen").write_text("stale\n")
        self.assertEqual(self.start()[0], 200)
        self.assertNotEqual((self.state / "hooks" / "publisher.seen").read_text().strip(), "stale")
        stopped = self.stop()[1]
        self.assertFalse((self.state / "hooks" / "publisher.seen").exists())
        self.assertTrue(stopped["publisherConnected"])

    def test_commands_are_idempotent_and_keys_cannot_change_meaning(self) -> None:
        self.prepare_version("v1")
        first = self.start()[1]
        status, replay = self.start()
        self.assertEqual(status, 200)
        self.assertTrue(replay["commandResult"]["duplicate"])
        self.assertEqual(replay["sessionId"], first["sessionId"])
        for changed in (
            {"version": "v2"},
            {"selection": {**SELECTION, "version": "destinations-v2"}},
        ):
            with self.subTest(changed=list(changed)):
                status, payload = self.start(**changed)
                self.assertEqual((status, payload["error"]), (409, "idempotency key was reused for another command"))
        status, payload = self.stop(key="start-1", sequence=2)
        self.assertEqual((status, payload["error"]), (409, "idempotency key was reused for another command"))

    def test_sequences_must_increase(self) -> None:
        self.prepare_version("v1")
        self.assertEqual(self.start(sequence=5)[0], 200)
        status, payload = self.stop(key="stop-old", sequence=5)
        self.assertEqual((status, payload["error"], payload["lastAcceptedSequence"]), (409, "command sequence is not newer than the last accepted command", 5))

    def test_a_started_session_cannot_be_rebound(self) -> None:
        self.prepare_version("v1")
        self.prepare_version("v2")
        self.assertEqual(self.start()[0], 200)
        status, same = self.start(key="start-same", sequence=2)
        self.assertEqual((status, same["commandResult"]["result"]), (200, "already-started"))
        for key, sequence, options in (
            ("other-filler", 3, {"version": "v2"}),
            ("other-destinations", 4, {"selection": {"version": "destinations-v2", "destinations": [{"id": "sink-a", "secretId": "secret-b", "versionId": "version-b"}]}}),
        ):
            with self.subTest(key=key):
                status, payload = self.start(key=key, sequence=sequence, **options)
                self.assertEqual((status, payload["error"]), (409, "active session is bound to another immutable configuration"))
        self.cs.DESTINATION_RUNTIME.active = None
        status, payload = self.start(key="no-runtime", sequence=5)
        self.assertEqual((status, payload["error"]), (409, "active session is bound to another immutable configuration"))

    def test_start_requires_a_prepared_filler_version(self) -> None:
        for version in (None, "missing", "../escape"):
            with self.subTest(version=version):
                status, payload = self.start(key=f"start-{version}", version=version)
                self.assertEqual((status, payload["error"]), (409, "requested filler version is not prepared"))
        self.assertEqual(self.cs.requested_state(), "stopped")

    def test_duplicate_stop_is_harmless(self) -> None:
        status, payload = self.stop()
        self.assertEqual((status, payload["commandResult"]["result"]), (200, "already-stopped"))

    def test_start_failures_leave_the_relay_stopped(self) -> None:
        self.prepare_version("v1")
        cases = (
            ("secret-unavailable", {"version": "destinations-v1", "destinations": [{"id": "sink-a", "secretId": "absent", "versionId": "version-a"}]}, 503, "secret_unavailable"),
            ("invalid-payload", {"version": "destinations-v1", "destinations": [{"id": "sink-a", "secretId": "secret-a", "versionId": "bad-payload"}]}, 422, "unsupported_scheme"),
        )
        for index, (name, selection, expected_status, reason) in enumerate(cases, 1):
            with self.subTest(case=name):
                status, payload = self.start(key=name, sequence=index, selection=selection)
                self.assertEqual((status, payload["error"]), (expected_status, reason))
                self.assertEqual(payload["requestedState"], "stopped")

    def test_runtime_failures_report_their_reason_and_stay_stopped(self) -> None:
        self.prepare_version("v1")
        runtime = RecordingRuntime(DestinationError("supervisor_start_failed"))
        self.request_with_runtime(runtime)
        status, payload = self.start()
        self.assertEqual((status, payload["error"]), (422, "supervisor_start_failed"))
        self.assertEqual(runtime.calls, ["activate"])
        runtime.failure = DestinationError("provider_unavailable")
        status, payload = self.start(key="start-2", sequence=2)
        self.assertEqual((status, payload["error"]), (503, "provider_unavailable"))

    def test_state_write_failures_roll_the_runtime_back(self) -> None:
        self.prepare_version("v1")
        runtime = RecordingRuntime()
        self.request_with_runtime(runtime)
        real_write = self.cs.atomic_write

        def failing_write(path: Path, value: str) -> None:
            if path.name == "started.at":
                raise OSError("injected disk failure")
            real_write(path, value)

        with patch.object(self.cs, "atomic_write", failing_write):
            status, payload = self.start()
        self.assertEqual((status, payload["error"]), (503, "runtime_activation_failed"))
        self.assertEqual(runtime.calls, ["activate", "deactivate"])
        self.assertEqual(self.cs.requested_state(), "stopped")

    def test_stop_waits_for_supervisors_to_quiesce(self) -> None:
        self.prepare_version("v1")
        runtime = RecordingRuntime()
        self.request_with_runtime(runtime)
        self.assertEqual(self.start()[0], 200)
        self.write_destinations("relaying")
        sleeps: list[float] = []

        def supervisors_exit(duration: float) -> None:
            sleeps.append(duration)
            for path in self.state.glob("dest-*"):
                path.unlink()
            self.write_state("dest.count", "0")

        with patch.object(self.cs.time, "sleep", supervisors_exit):
            status, payload = self.stop()
        self.assertEqual(status, 200)
        self.assertEqual(sleeps, [0.1])
        self.assertEqual(payload["actualState"], "stopped")

    def test_stop_gives_up_waiting_after_its_deadline(self) -> None:
        self.prepare_version("v1")
        self.request_with_runtime(RecordingRuntime())
        self.assertEqual(self.start()[0], 200)
        self.write_destinations("relaying")
        clock = iter([0.0, 1.0, 100.0])
        with patch.object(self.cs.time, "monotonic", lambda: next(clock)), patch.object(self.cs.time, "sleep"):
            status, payload = self.cs.apply_command("stop", "stop-late", 2, None, None)
        self.assertEqual(status, 200)
        self.assertEqual(payload["actualState"], "stopping")

    def test_start_without_a_destination_selection_is_refused(self) -> None:
        status, payload = self.cs.apply_command("start", "k", 1, "v1", None)
        self.assertEqual((status, payload["error"]), (400, "a bounded destination selection is required"))


UUID_PATTERN = __import__("re").compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


class CommandValidationTest(ControlServerCase):
    def post(self, suffix: str = "/start", **kwargs) -> tuple[int, dict]:
        status, payload, _ = self.call("POST", self.session_url(suffix), **kwargs)
        return status, payload

    def test_malformed_commands_are_rejected_before_state_changes(self) -> None:
        body = SELECTION
        cases = {
            "missing-key": ({"headers": {"X-Command-Sequence": "1"}, "body": body}, "a bounded Idempotency-Key is required"),
            "oversized-key": ({"headers": {"Idempotency-Key": "k" * 201, "X-Command-Sequence": "1"}, "body": body}, "a bounded Idempotency-Key is required"),
            "sequence-text": ({"headers": {"Idempotency-Key": "k", "X-Command-Sequence": "first"}, "body": body}, "X-Command-Sequence must be a positive integer"),
            "sequence-zero": ({"headers": {"Idempotency-Key": "k", "X-Command-Sequence": "0"}, "body": body}, "X-Command-Sequence must be a positive integer"),
            "sequence-missing": ({"headers": {"Idempotency-Key": "k"}, "body": body}, "X-Command-Sequence must be a positive integer"),
            "no-body": ({"headers": {"Idempotency-Key": "k", "X-Command-Sequence": "1"}}, "a bounded destination selection is required"),
            "bad-length": ({"headers": {"Idempotency-Key": "k", "X-Command-Sequence": "1", "Content-Length": "abc"}}, "a bounded destination selection is required"),
            "huge-body": ({"headers": {"Idempotency-Key": "k", "X-Command-Sequence": "1", "Content-Length": "70000"}}, "a bounded destination selection is required"),
            "not-json": ({"headers": {"Idempotency-Key": "k", "X-Command-Sequence": "1"}, "raw": b"{nope"}, "invalid destination selection"),
            "invalid-selection": ({"headers": {"Idempotency-Key": "k", "X-Command-Sequence": "1"}, "body": {"version": "v", "destinations": []}}, "invalid destination selection"),
        }
        for name, (kwargs, message) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(self.post(**kwargs), (400, {"error": message}))
        self.assertEqual(self.cs.requested_state(), "stopped")
        self.assertEqual(self.cs.DESTINATION_METRICS["invalid"], 2)

    def test_stop_needs_no_body(self) -> None:
        self.assertEqual(self.post("/stop", headers={"Idempotency-Key": "k", "X-Command-Sequence": "1"})[0], 200)


class RealRuntimeWiringTest(ControlServerCase):
    def test_start_launches_real_supervisor_processes_and_stop_reaps_them(self) -> None:
        self.prepare_version("v1")
        selection = {
            "version": "destinations-v2",
            "destinations": [
                {"id": "sink-a", "secretId": "secret-a", "versionId": "version-a"},
                {"id": "sink-b", "secretId": "secret-b", "versionId": "version-b"},
            ],
        }
        status, payload = self.start(selection=selection)
        self.assertEqual(status, 200, payload)
        self.assertEqual([item["supervisorHealthy"] for item in payload["destinations"]], [True, True])
        self.assertEqual(self.cs.DESTINATION_RUNTIME.public_ids(), ["sink-a", "sink-b"])
        processes = list(self.cs.DESTINATION_RUNTIME.processes)
        self.assertEqual(self.stop()[1]["actualState"], "stopped")
        self.assertTrue(all(process.poll() is not None for process in processes))

    def test_failing_supervisor_aborts_the_start(self) -> None:
        self.prepare_version("v1")
        self.supervisor.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        # Use the runtime's real settle pause so the early exit is observed.
        with patch.object(destination_runtime, "time", time):
            status, payload = self.start()
        self.assertEqual((status, payload["error"]), (422, "supervisor_start_failed"))
        self.assertEqual(payload["requestedState"], "stopped")

    def test_provider_is_created_once_and_reused(self) -> None:
        first = self.cs.destination_provider()
        self.assertIs(self.cs.destination_provider(), first)


class ProcessEntryPointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        token = self.root / "token"
        token.write_text("t\n", encoding="utf-8")
        self.environment = {
            "PROGRAM_ID": PROGRAM,
            "STATE_DIR": str(self.root / "state"),
            "FILLER_STORE_DIR": str(self.root / "fillers"),
            "CONTROL_TOKEN_FILE": str(token),
            "CONTROL_BIND": "127.0.0.1",
            "CONTROL_PORT": "18081",
        }

        class FakeServer:
            instances: list["FakeServer"] = []

            def __init__(self, address: tuple[str, int], handler: type) -> None:
                self.address, self.handler, self.served = address, handler, False
                FakeServer.instances.append(self)

            def serve_forever(self) -> None:
                self.served = True

        self.server_class = FakeServer
        self.output = io.StringIO()
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(self.output))
        self.handlers: dict[int, object] = {}
        stack.enter_context(patch.object(signal, "signal", lambda number, handler: self.handlers.__setitem__(number, handler)))

    def test_main_prepares_state_registers_signals_and_serves(self) -> None:
        module = load_server(self.environment)
        module.ThreadingHTTPServer = self.server_class
        runtime = RecordingRuntime()
        module.DESTINATION_RUNTIME = runtime
        module.main()
        control = Path(self.environment["STATE_DIR"]) / "control"
        self.assertEqual((control / "requested.state").read_text().strip(), "stopped")
        self.assertTrue((control / "commands").is_dir() and (control / "destination-commands").is_dir())
        server = self.server_class.instances[-1]
        self.assertEqual((server.address, server.handler, server.served), (("127.0.0.1", 18081), module.Handler, True))
        self.assertIn(f"listening on 127.0.0.1:18081 for program={PROGRAM}", self.output.getvalue())
        self.assertEqual(set(self.handlers), {signal.SIGTERM, signal.SIGINT})
        for number in (signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=number), self.assertRaises(SystemExit) as raised:
                self.handlers[number](number, None)
            self.assertEqual(raised.exception.code, 0)
        self.assertEqual(runtime.calls, ["deactivate", "deactivate"])

    def test_main_preserves_an_existing_requested_state(self) -> None:
        module = load_server(self.environment)
        module.ThreadingHTTPServer = self.server_class
        control = Path(self.environment["STATE_DIR"]) / "control"
        control.mkdir(parents=True)
        (control / "requested.state").write_text("started\n", encoding="utf-8")
        module.main()
        self.assertEqual((control / "requested.state").read_text().strip(), "started")

    def test_running_the_file_as_a_script_starts_the_server(self) -> None:
        with patch.dict(os.environ, self.environment, clear=False), patch("http.server.ThreadingHTTPServer", self.server_class):
            namespace = runpy.run_path(str(SERVER_PATH), run_name="__main__")
        self.assertTrue(self.server_class.instances[-1].served)
        self.assertIn("main", namespace)

    def test_importing_the_module_does_not_start_a_server(self) -> None:
        load_server(self.environment)
        self.assertEqual(self.server_class.instances, [])


if __name__ == "__main__":
    unittest.main()
