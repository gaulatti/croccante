#!/usr/bin/env python3
from __future__ import annotations

import functools
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from filler_store import FillerStore, PreparationError


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        return


class FillerStoreRuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.assets = cls.root / "assets"
        cls.assets.mkdir()
        cls._ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=160x90", "-frames:v", "1", str(cls.assets / "still.bmp"))
        cls._ffmpeg("-f", "lavfi", "-i", "testsrc=s=160x90:r=10", "-t", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(cls.assets / "silent.mp4"))
        cls._ffmpeg(
            "-f", "lavfi", "-i", "testsrc=s=160x90:r=10", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
            "-t", "1", "-c:v", "libvpx-vp9", "-c:a", "libopus", str(cls.assets / "audio.webm"),
        )
        handler = functools.partial(QuietHandler, directory=str(cls.assets))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    @classmethod
    def _ffmpeg(cls, *args: str) -> None:
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)

    def setUp(self) -> None:
        self.store = FillerStore(self.root / self.id().rsplit(".", 1)[-1], "program-a")

    def request(self, asset: str, command: str = "command-a") -> dict[str, object]:
        path = self.assets / asset
        checksum = hashlib.sha256(path.read_bytes()).hexdigest()
        return {
            "commandId": command,
            "source": {
                "id": f"source-{asset}", "sha256": checksum,
                "downloadUrl": f"http://127.0.0.1:{self.server.server_port}/{asset}?signed=private",
            },
            "profile": {
                "width": 160, "height": 90, "fps": 10, "videoBitrate": "200k",
                "audioRate": 44100, "audioChannels": 2, "audioBitrate": "64k",
                "gop": 10, "loopSeconds": 2,
            },
        }

    def assert_prepared(self, asset: str, version: str) -> dict[str, object]:
        result = self.store.prepare(version, self.request(asset), "2026-08-24T00:00:00Z")
        self.assertTrue(result["ready"])
        self.assertNotIn("downloadUrl", json.dumps(result))
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(self.store.version_dir(version) / "filler.flv")],
            check=True, capture_output=True, text=True,
        )
        streams = json.loads(probe.stdout)["streams"]
        self.assertEqual({stream["codec_type"] for stream in streams}, {"video", "audio"})
        return result

    def test_still_image_gains_video_and_silent_audio(self) -> None:
        self.assert_prepared("still.bmp", "still-v1")

    def test_silent_video_gains_audio(self) -> None:
        self.assert_prepared("silent.mp4", "silent-v1")

    def test_webm_preserves_audio_content_path(self) -> None:
        self.assert_prepared("audio.webm", "webm-v1")

    def test_idempotency_restart_revalidation_and_conflict(self) -> None:
        first = self.assert_prepared("still.bmp", "version-a")
        duplicate = self.store.prepare("version-a", self.request("still.bmp"), "later")
        self.assertEqual(first["artifactSha256"], duplicate["artifactSha256"])
        refreshed_url = self.request("still.bmp")
        refreshed_url["source"]["downloadUrl"] += "&refreshed=yes"  # type: ignore[index,operator]
        self.assertTrue(self.store.prepare("version-a", refreshed_url, "later")["ready"])
        restarted = FillerStore(self.store.root, "program-a")
        self.assertTrue(restarted.public_state("version-a")["ready"])
        with self.assertRaisesRegex(PreparationError, "version-conflict"):
            restarted.prepare("version-a", self.request("silent.mp4", "command-b"), "later")
        self.assertTrue(restarted.public_state("version-a")["ready"])

    def test_checksum_and_download_failures_preserve_known_good(self) -> None:
        self.assert_prepared("still.bmp", "good")
        bad = self.request("silent.mp4")
        bad["source"]["sha256"] = "0" * 64  # type: ignore[index]
        with self.assertRaisesRegex(PreparationError, "checksum-mismatch"):
            self.store.prepare("bad", bad, "now")
        missing = self.request("silent.mp4")
        missing["source"]["downloadUrl"] = f"http://127.0.0.1:{self.server.server_port}/missing"  # type: ignore[index]
        with self.assertRaisesRegex(PreparationError, "download-failed"):
            self.store.prepare("missing", missing, "now")
        self.assertTrue(self.store.public_state("good")["ready"])

    def test_cleanup_never_removes_active_version(self) -> None:
        for index in range(5):
            self.store.prepare(f"v{index}", self.request("still.bmp", f"c{index}"), f"time-{index}")
        self.store.cleanup("v0", retain=2)
        self.assertTrue(self.store.public_state("v0")["ready"])
        ready = [self.store.public_state(f"v{index}")["ready"] for index in range(5)]
        self.assertEqual(sum(ready), 3)

    def test_authenticated_api_prepares_and_binds_immutable_session_version(self) -> None:
        state = self.root / "api-state"
        token = self.root / "api-token"
        token.write_text("private-test-token\n")
        secrets = self.root / "destination-secrets.json"
        secrets.write_text(json.dumps({
            "secret-a": {
                "version-a": {
                    "scheme": "rtmp", "host": "127.0.0.1", "port": 19350,
                    "application": "live", "streamKey": "fictional-key",
                }
            }
        }))
        supervisor = self.root / "supervisor.sh"
        supervisor.write_text("#!/bin/sh\ntrap 'exit 0' TERM INT\nwhile true; do sleep 1; done\n")
        supervisor.chmod(0o700)
        with socket.socket() as candidate:
            candidate.bind(("127.0.0.1", 0))
            port = candidate.getsockname()[1]
        environment = {
            **os.environ,
            "PROGRAM_ID": "program-a",
            "STATE_DIR": str(state),
            "FILLER_STORE_DIR": str(self.root / "api-fillers"),
            "CONTROL_TOKEN_FILE": str(token),
            "CONTROL_BIND": "127.0.0.1",
            "CONTROL_PORT": str(port),
            "CROCCANTE_ENVIRONMENT": "test",
            "DESTINATION_SECRET_PROVIDER": "file",
            "DESTINATION_FAKE_SECRETS_FILE": str(secrets),
            "RELAY_SUPERVISOR": str(supervisor),
        }
        server = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve().parents[1] / "control-server.py")],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )

        def call(method: str, path: str, *, body: object | None = None, headers: dict[str, str] | None = None) -> tuple[int, str]:
            encoded = json.dumps(body).encode() if body is not None else (b"" if method == "POST" else None)
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}", data=encoded, method=method,
                headers={"Authorization": "Bearer private-test-token", **(headers or {})},
            )
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    return response.status, response.read().decode()
            except urllib.error.HTTPError as error:
                with error:
                    return error.code, error.read().decode()

        try:
            selection = {
                "version": "destinations-v1",
                "destinations": [
                    {"id": "sink-a", "secretId": "secret-a", "versionId": "version-a"}
                ],
            }
            for _ in range(50):
                try:
                    if call("GET", "/v1/programs/program-a/session")[0] == 200:
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                self.fail("control server did not start")
            configuration = {
                "commandId": "destinations-prepare-1",
                **selection,
            }
            configured = call(
                "PUT", "/v1/programs/program-a/destinations/destinations-v1",
                body=configuration,
                headers={"Idempotency-Key": "destinations-prepare-1", "Content-Type": "application/json"},
            )
            self.assertEqual(configured[0], 200, configured[1])
            self.assertNotIn("secret-a", configured[1])
            configured_replay = call(
                "PUT", "/v1/programs/program-a/destinations/destinations-v1",
                body=configuration,
                headers={"Idempotency-Key": "destinations-prepare-1", "Content-Type": "application/json"},
            )
            self.assertEqual(configured_replay[0], 200)
            self.assertTrue(json.loads(configured_replay[1])["duplicate"])
            unprepared = call(
                "POST", "/v1/programs/program-a/session/start", body=selection,
                headers={"Idempotency-Key": "start-0", "X-Command-Sequence": "1", "X-Filler-Version": "missing"},
            )
            self.assertEqual(unprepared[0], 409)
            for version, command in (("v1", "prepare-1"), ("v2", "prepare-2")):
                payload = self.request("still.bmp", command)
                status, response = call(
                    "PUT", f"/v1/programs/program-a/fillers/{version}", body=payload,
                    headers={"Idempotency-Key": command, "Content-Type": "application/json"},
                )
                self.assertEqual(status, 200, response)
                self.assertNotIn("downloadUrl", response)
            started = call(
                "POST", "/v1/programs/program-a/session/start", body=selection,
                headers={"Idempotency-Key": "start-1", "X-Command-Sequence": "1", "X-Filler-Version": "v1"},
            )
            self.assertEqual(started[0], 200)
            secrets.unlink()
            still_running = call("GET", "/v1/programs/program-a/session")
            self.assertEqual(still_running[0], 200)
            self.assertEqual(json.loads(still_running[1])["requestedState"], "started")
            changed_selection = {
                "version": "destinations-v2",
                "destinations": [
                    {"id": "another-opaque-id", "secretId": "secret-a", "versionId": "version-a"}
                ],
            }
            changed = call(
                "POST", "/v1/programs/program-a/session/start", body=changed_selection,
                headers={"Idempotency-Key": "start-changed", "X-Command-Sequence": "2", "X-Filler-Version": "v1"},
            )
            self.assertEqual(changed[0], 409)
            reconfigured_active = call(
                "PUT", "/v1/programs/program-a/destinations/destinations-v2",
                body={"commandId": "destinations-prepare-2", **changed_selection},
                headers={"Idempotency-Key": "destinations-prepare-2", "Content-Type": "application/json"},
            )
            self.assertEqual(reconfigured_active[0], 409)
            switched = call(
                "POST", "/v1/programs/program-a/session/start", body=selection,
                headers={"Idempotency-Key": "start-2", "X-Command-Sequence": "3", "X-Filler-Version": "v2"},
            )
            self.assertEqual(switched[0], 409)
            metrics = call("GET", "/metrics")
            self.assertEqual(metrics[0], 200)
            self.assertIn('outcome="success"', metrics[1])
            self.assertNotIn("v1", metrics[1])
            self.assertNotIn("source-still", metrics[1])
        finally:
            server.terminate()
            server.wait(timeout=5)
            if server.stdout is not None:
                server.stdout.close()


if __name__ == "__main__":
    unittest.main()
