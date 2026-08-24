import importlib.util
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

os.environ["PROGRAM_ID"] = "bounded-test-program"

import relay_metrics


SAMPLE_PATTERN = re.compile(
    r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)"
    r"(?:\{(?P<labels>[^}]*)\})? "
    r"(?P<value>-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)$"
)


def parse_exposition(payload: str) -> list[tuple[str, dict[str, str], float]]:
    """Strictly parse the sample subset emitted by Croccante."""

    samples = []
    seen = set()
    for line in payload.splitlines():
        if not line or line.startswith("#"):
            continue
        match = SAMPLE_PATTERN.fullmatch(line)
        if not match:
            raise AssertionError(f"invalid Prometheus sample: {line}")
        labels = {}
        if match.group("labels"):
            for item in match.group("labels").split(","):
                key, quoted = item.split("=", 1)
                if not quoted.startswith('"') or not quoted.endswith('"'):
                    raise AssertionError(f"invalid label: {item}")
                labels[key] = quoted[1:-1]
        identity = (match.group("name"), tuple(sorted(labels.items())))
        if identity in seen:
            raise AssertionError(f"duplicate Prometheus series: {identity}")
        seen.add(identity)
        samples.append((match.group("name"), labels, float(match.group("value"))))
    return samples


class MetricsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temporary.name)
        (self.state_dir / "control" / "commands").mkdir(parents=True)
        (self.state_dir / "hooks").mkdir()
        (self.state_dir / "metrics" / "hooks").mkdir(parents=True)
        (self.state_dir / "control" / "requested.state").write_text("started\n")
        (self.state_dir / "hooks" / "publisher").write_text("private-stream-name\n")
        (self.state_dir / "hooks" / "publisher.seen").write_text("1\n")
        (self.state_dir / "dest.count").write_text("22\n")
        for index in range(1, 23):
            state = "relaying" if index == 1 else "backoff" if index == 2 else "idle"
            (self.state_dir / f"dest-{index}.state").write_text(f"{state}\n")
            (self.state_dir / "metrics" / f"dest-{index}.attempt.total").write_text(
                f"{index}\n"
            )
        (self.state_dir / "metrics" / "dest-2.retry.total").write_text("3\n")
        (self.state_dir / "metrics" / "dest-2.result-failure.total").write_text("3\n")
        (self.state_dir / "metrics" / "dest-2.backoff.seconds").write_text("4\n")
        (self.state_dir / "metrics" / "hooks" / "publisher-connect.total").write_text(
            "1\n"
        )
        relay_metrics.STATE_DIR = self.state_dir
        relay_metrics.BUILD_VERSION = "build-test"
        relay_metrics._control_counts.clear()
        relay_metrics._control_duration_sums.clear()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_collector_parses_bounded_success_and_failure_output(self) -> None:
        relay_metrics.record_control_request("GET", "metrics", "success", 0.02)
        relay_metrics.record_control_request(
            "PRIVATE-METHOD", "private-url", "private-error", 0.03
        )
        payload = relay_metrics.collect()
        samples = parse_exposition(payload)

        names = {name for name, _, _ in samples}
        self.assertTrue(
            {
                "croccante_build_info",
                "croccante_process_cpu_seconds_total",
                "croccante_ingest_publisher_connected",
                "croccante_relay_destinations_active",
                "croccante_relay_slot_state",
                "croccante_relay_retries_total",
                "croccante_relay_results_total",
                "croccante_relay_backoff_seconds",
                "croccante_control_requests_total",
            }
            <= names
        )
        allowed_keys = {
            "event",
            "method",
            "result",
            "route",
            "service",
            "slot",
            "state",
            "version",
        }
        self.assertTrue(all(set(labels) <= allowed_keys for _, labels, _ in samples))
        self.assertIn(
            ("croccante_relay_results_total", {"result": "failure", "slot": "2"}, 3.0),
            samples,
        )
        self.assertTrue(
            any(labels.get("slot") == "overflow" for _, labels, _ in samples)
        )
        self.assertIn(
            (
                "croccante_control_requests_total",
                {"method": "unknown", "result": "unknown", "route": "unknown"},
                1.0,
            ),
            samples,
        )
        self.assertNotIn("private-stream-name", payload)
        self.assertNotIn("private-url", payload)
        self.assertNotIn("private-error", payload)

    def test_http_metrics_boundary_rejects_bad_tokens_and_serves_real_output(
        self,
    ) -> None:
        token_file = self.state_dir / "control-token"
        token_file.write_text("bounded-test-token\n")
        os.environ["STATE_DIR"] = str(self.state_dir)
        os.environ["CONTROL_TOKEN_FILE"] = str(token_file)
        os.environ["FILLER_STORE_DIR"] = str(self.state_dir / "fillers")

        module_path = Path(__file__).parent.parent / "control-server.py"
        spec = importlib.util.spec_from_file_location(
            "control_server_test", module_path
        )
        control_server = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(control_server)
        control_server.STATE_DIR = self.state_dir
        control_server.CONTROL_DIR = self.state_dir / "control"
        control_server.HOOK_DIR = self.state_dir / "hooks"
        control_server.TOKEN_FILE = token_file

        server = control_server.ThreadingHTTPServer(
            ("127.0.0.1", 0), control_server.Handler
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = f"http://127.0.0.1:{server.server_port}/metrics"

        try:
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(url)
            self.assertEqual(missing.exception.code, 401)
            self.assertEqual(missing.exception.headers["WWW-Authenticate"], "Bearer")
            missing.exception.close()

            wrong = urllib.request.Request(
                url, headers={"Authorization": "Bearer private-wrong-token"}
            )
            with self.assertRaises(urllib.error.HTTPError) as rejected:
                urllib.request.urlopen(wrong)
            self.assertEqual(rejected.exception.code, 401)
            rejected.exception.close()

            valid = urllib.request.Request(
                url, headers={"Authorization": "Bearer bounded-test-token"}
            )
            first = urllib.request.urlopen(valid)
            self.assertEqual(first.status, 200)
            self.assertTrue(first.headers["Content-Type"].startswith("text/plain"))
            second = urllib.request.urlopen(valid)
            payload = second.read().decode("utf-8")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        samples = parse_exposition(payload)
        request_samples = [
            sample
            for sample in samples
            if sample[0] == "croccante_control_requests_total"
        ]
        self.assertTrue(
            any(
                labels.get("result") == "unauthorized"
                for _, labels, _ in request_samples
            )
        )
        self.assertTrue(
            any(labels.get("result") == "success" for _, labels, _ in request_samples)
        )
        self.assertNotIn("bounded-test-token", payload)
        self.assertNotIn("private-wrong-token", payload)


if __name__ == "__main__":
    unittest.main()
