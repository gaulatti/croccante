"""Contract rejection, actual encoding and restart checks for opt-in profiles."""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from filler_store import FillerStore, PreparationError, Profile
import test_filler_store as legacy

BASE = {
    "width": 160, "height": 90, "fps": 10, "videoBitrate": "200k",
    "audioRate": 44100, "audioChannels": 2, "audioBitrate": "64k",
    "gop": 10, "loopSeconds": 2,
}
EXTRA = {
    "contractVersion": 2, "videoMinrate": None, "videoMaxrate": "250k",
    "videoBufsize": "400k", "h264Profile": "high444", "pixelFormat": "yuv444p",
    "h264Level": 10, "maxBFrames": 0, "minKeyint": 1, "sceneThreshold": 0,
    "encoderPreset": "veryfast",
}
FULL = {**BASE, **EXTRA}
VIDEO = {
    "codec_type": "video", "codec_name": "h264", "width": 160, "height": 90,
    "avg_frame_rate": "10/1", "pix_fmt": "yuv444p", "profile": "High 4:4:4 Predictive",
    "level": 10, "has_b_frames": 0,
}
AUDIO = {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2}


class ExactProfileContractTest(unittest.TestCase):
    def test_off_retains_legacy_coercions_defaults_and_acknowledgement(self):
        old = {**BASE, "width": "160"}
        old.pop("loopSeconds")
        profile = Profile.parse({**old, **EXTRA})
        self.assertIsNone(profile.encoding)
        self.assertEqual(profile.public(), {**BASE, "loopSeconds": 10})

    def test_complete_typed_profile_round_trips_without_dropping_configuration(self):
        raw = copy.deepcopy(FULL)
        profile = Profile.parse(raw, exact=True)
        self.assertEqual(profile.public(), raw)
        raw["videoMaxrate"] = "999k"
        self.assertEqual(profile.public()["videoMaxrate"], "250k")

    def test_every_required_field_unknown_field_and_legacy_coercion_fail(self):
        for key in FULL:
            with self.subTest(missing=key), self.assertRaisesRegex(PreparationError, "invalid-profile"):
                Profile.parse({k: v for k, v in FULL.items() if k != key}, exact=True)
        for update in ({"extra": "ignored"}, {"width": "160"}, {"fps": True}, {"fps": float("inf")}):
            with self.subTest(update=update), self.assertRaisesRegex(PreparationError, "invalid-profile"):
                Profile.parse({**FULL, **update}, exact=True)

    def test_bad_contract_and_encoding_inputs_fail_before_preparation(self):
        invalid = [
            ("contractVersion", True), ("contractVersion", 1),
            ("videoMinrate", 1), ("videoMinrate", "not-a-rate"), ("videoMinrate", "300k"),
            ("videoMaxrate", None), ("videoMaxrate", "199k"), ("videoBufsize", "0k"),
            ("h264Profile", []), ("h264Profile", "unknown"), ("pixelFormat", "yuv420p"),
            ("h264Level", True), ("h264Level", 9), ("h264Level", 63), ("h264Level", 14),
            ("maxBFrames", -1), ("maxBFrames", 17), ("maxBFrames", 0.0),
            ("minKeyint", 0), ("minKeyint", 11),
            ("sceneThreshold", -1), ("sceneThreshold", 101),
            ("encoderPreset", []), ("encoderPreset", "not-a-preset"),
        ]
        for key, value in invalid:
            with self.subTest(key=key, value=value), self.assertRaisesRegex(PreparationError, "invalid-profile"):
                Profile.parse({**FULL, key: value}, exact=True)
        with self.assertRaisesRegex(PreparationError, "invalid-profile"):
            Profile.parse({**FULL, "h264Profile": "baseline", "pixelFormat": "yuv420p", "maxBFrames": 1}, exact=True)

    def test_encoder_arguments_distinguish_absent_minrate_and_explicit_rate(self):
        profile = Profile.parse(FULL, exact=True)
        with patch("filler_store.subprocess.run") as run:
            FillerStore._transcode(Path("in"), Path("out"), profile, {"streams": [VIDEO, AUDIO]})
        args = run.call_args.args[0]
        self.assertNotIn("-minrate", args)
        for option, expected in {
            "-maxrate": "250k", "-bufsize": "400k", "-profile:v": "high444",
            "-pix_fmt": "yuv444p", "-level:v": "1.0", "-bf": "0",
            "-keyint_min": "1", "-sc_threshold": "0", "-tune": "zerolatency",
        }.items():
            self.assertEqual(args[args.index(option) + 1], expected)
        encoding = Profile.parse({**FULL, "videoMinrate": "100k"}, exact=True).encoding
        args = encoding.arguments("200k", 10)
        self.assertEqual(args[args.index("-minrate") + 1], "100k")
        self.assertEqual(Profile.parse({**FULL, "videoMaxrate": "1M", "videoBufsize": "2M"}, exact=True).encoding.maximum, "1M")

    def test_observed_stream_mismatches_never_validate_as_ready(self):
        profile = Profile.parse(FULL, exact=True)
        FillerStore._validate_output({"streams": [VIDEO, AUDIO]}, profile)
        for key, value in [
            ("profile", "Main"), ("profile", []), ("level", 12), ("level", "10"),
            ("has_b_frames", True), ("has_b_frames", -1), ("has_b_frames", 1),
            ("pix_fmt", "yuv420p"),
        ]:
            with self.subTest(key=key, value=value), self.assertRaisesRegex(PreparationError, "profile-mismatch"):
                FillerStore._validate_output({"streams": [{**VIDEO, key: value}, AUDIO]}, profile)

    def test_baseline_and_other_supported_profile_metadata_are_explicit(self):
        for name, observed in [("baseline", "Constrained Baseline"), ("baseline", "Baseline"), ("main", "Main"), ("high", "High")]:
            raw = {**FULL, "h264Profile": name, "pixelFormat": "yuv420p"}
            profile = Profile.parse(raw, exact=True)
            FillerStore._validate_output({"streams": [{**VIDEO, "pix_fmt": "yuv420p", "profile": observed}, AUDIO]}, profile)


class ExactFillerRuntimeTest(legacy.FillerStoreRuntimeTest):
    # Run the existing real image/silent-video/WebM, idempotency, immutable-source,
    # checksum, download, restart and program-isolation contract in both modes.
    def setUp(self):
        self.store = FillerStore(self.root / self.id().rsplit(".", 1)[-1], "program-a", exact_profiles=True)

    def request(self, asset, command="command-a"):
        return {**super().request(asset, command), "profile": dict(FULL)}

    def assert_decodes(self, path):
        # Decode every video/audio packet, not just the readable stream headers.
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-xerror",
            "-err_detect", "explode", "-i", str(path),
            "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-",
        ], check=True, capture_output=True, text=True, timeout=30)

    def assert_prepared(self, asset, version):
        result = super().assert_prepared(asset, version)
        self.assert_decodes(self.store.version_dir(version) / "filler.flv")
        return result

    def test_full_decode_rejects_corrupt_packets_with_readable_stream_headers(self):
        self.assert_prepared("audio.webm", "decode-control")
        path = self.store.version_dir("decode-control") / "filler.flv"
        data = bytearray(path.read_bytes())
        position = int.from_bytes(data[5:9], "big") + 4
        damaged = 0
        while position + 15 <= len(data):
            size = int.from_bytes(data[position + 1:position + 4], "big")
            end = position + 11 + size
            if end + 4 > len(data):
                self.fail("Fixture contains an incomplete FLV tag")
            if data[position] == 9 and size > 9 and data[position + 12] == 1:
                data[position + 16:end] = b"\xff" * (end - position - 16)
                damaged += 1
            position = end + 4
        self.assertGreater(damaged, 0)
        corrupt = self.store.root / "readable-headers-corrupt-packets.flv"
        corrupt.write_bytes(data)
        probe = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-of", "json", str(corrupt),
        ], check=True, capture_output=True, text=True)
        self.assertEqual({stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]},
                         {"video", "audio"})
        with self.assertRaises(subprocess.CalledProcessError):
            self.assert_decodes(corrupt)

    def test_complete_profile_is_real_and_ready_after_offline_restart(self):
        result = self.assert_prepared("audio.webm", "exact-v2")
        self.assertEqual(result["profile"], FULL)
        self.assertTrue(self.store.ready_for_start("exact-v2"))
        restarted = FillerStore(self.store.root, "program-a", exact_profiles=True)
        self.assertTrue(restarted.ready_for_start("exact-v2"))
        self.assertFalse(restarted.ready_for_start("missing"))
        video = FillerStore._probe(self.store.version_dir("exact-v2") / "filler.flv")["streams"][0]
        self.assertEqual((video["profile"], video["pix_fmt"], video["level"], video["has_b_frames"]),
                         ("High 4:4:4 Predictive", "yuv444p", 10, 0))
        # A complete typed manifest alone cannot make a mismatching stream ready.
        with patch.object(restarted, "_probe", return_value={"streams": [{**VIDEO, "profile": "Main"}, AUDIO]}):
            self.assertFalse(restarted.ready_for_start("exact-v2"))

    def test_legacy_artifact_remains_intact_but_cannot_start_as_an_exact_profile(self):
        legacy = FillerStore(self.store.root, "program-a")
        request = super().request("still.bmp")
        request["profile"] = dict(BASE)
        before = legacy.prepare("legacy", request, "now")
        self.assertTrue(legacy.ready_for_start("legacy"))
        self.assertFalse(self.store.ready_for_start("legacy"))
        self.assertEqual(self.store.public_state("legacy"), before)
        self.assertTrue(self.store.manifest("legacy"))
        with self.assertRaisesRegex(PreparationError, "version-conflict"):
            self.store.prepare("legacy", self.request("still.bmp"), "later")
        self.assertEqual(self.store.public_state("legacy"), before)

    def test_prepare_with_incomplete_profile_does_not_read_source_or_replace_good(self):
        before = self.assert_prepared("still.bmp", "good-v2")
        request = self.request("silent.mp4")
        request["profile"].pop("videoBufsize")
        with patch("filler_store.urllib.request.urlopen") as download:
            with self.assertRaisesRegex(PreparationError, "invalid-profile"):
                self.store.prepare("bad-v2", request, "later")
        download.assert_not_called()
        self.assertEqual(self.store.public_state("good-v2"), before)

    def test_minrate_and_nonzero_reordering_profile_are_really_encodable(self):
        request = self.request("silent.mp4")
        request["profile"].update(h264Profile="main", pixelFormat="yuv420p", h264Level=12,
                                  maxBFrames=3, videoMinrate="100k")
        result = self.store.prepare("main-v2", request, "now")
        self.assertEqual(result["profile"], request["profile"])
        self.assertTrue(self.store.ready_for_start("main-v2"))
        self.assert_decodes(self.store.version_dir("main-v2") / "filler.flv")


# Reuse the normal private HTTP fixture (real token-file authentication, command
# journal and state machine). Its destination supervisor is explicitly local.
import functools
import hashlib
import os
import subprocess
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import test_control_server as control


class ExactProfileHTTPTest(control.ControlServerCase):
    @classmethod
    def setUpClass(cls):
        cls.assets_temp = tempfile.TemporaryDirectory()
        cls.assets_dir = Path(cls.assets_temp.name)
        cls.image = cls.assets_dir / "still.bmp"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                        "color=c=blue:s=160x90", "-frames:v", "1", str(cls.image)], check=True)
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *_args):
                pass
        cls.source_server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(cls.assets_dir)))
        cls.source_thread = threading.Thread(target=cls.source_server.serve_forever, daemon=True)
        cls.source_thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.source_server.shutdown()
        cls.source_server.server_close()
        cls.source_thread.join(timeout=5)
        cls.assets_temp.cleanup()

    def setUp(self):
        flag = patch.dict(os.environ, {"CROCCANTE_EXACT_FILLER_PROFILE": "1"})
        flag.start()
        self.addCleanup(flag.stop)
        super().setUp()
        self.assertTrue(self.cs.FILLER_STORE.exact_profiles)

    def payload(self):
        return {
            "commandId": "exact-http-command",
            "source": {"id": "owned-fixture", "sha256": hashlib.sha256(self.image.read_bytes()).hexdigest(),
                       "downloadUrl": f"http://127.0.0.1:{self.source_server.server_port}/still.bmp"},
            "profile": dict(FULL),
        }

    def put(self, version, payload, **kwargs):
        return self.call("PUT", f"/v1/programs/{control.PROGRAM}/fillers/{version}", body=payload,
                         headers={"Idempotency-Key": payload["commandId"]}, **kwargs)

    def test_authenticated_real_preparation_start_and_capability(self):
        status, state, _ = self.call("GET", self.session_url())
        self.assertEqual((status, state["fillerProfileContractVersion"]), (200, 2))
        status, result, _ = self.put("v2", self.payload())
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ready"])
        self.assertEqual(result["profile"], FULL)
        self.assertNotIn("downloadUrl", str(result))
        status, result = self.start(version="v2")
        self.assertEqual(status, 200, result)
        self.assertEqual((self.cs.CONTROL_DIR / "active-filler.version").read_text().strip(), "v2")
        self.assertEqual(self.stop()[0], 200)

    def test_bad_profile_and_identity_cannot_prepare_or_replace_known_good(self):
        self.assertEqual(self.put("good", self.payload())[0], 200)
        before = self.cs.FILLER_STORE.public_state("good")
        payload = self.payload()
        payload["profile"].pop("videoBufsize")
        status, failure, _ = self.put("bad", payload)
        self.assertEqual(status, 422)
        self.assertEqual(failure["reason"], "invalid-profile")
        self.assertEqual(self.put("denied", self.payload(), token=None)[0], 401)
        status, _, _ = self.call("PUT", "/v1/programs/other/fillers/wrong", body=self.payload(),
                                  headers={"Idempotency-Key": "exact-http-command"})
        self.assertEqual(status, 404)
        self.assertEqual(self.cs.FILLER_STORE.public_state("good"), before)
        self.assertIsNone(self.cs.FILLER_STORE.manifest("bad"))

    def test_legacy_prepared_media_does_not_authorize_an_exact_start(self):
        legacy_store = FillerStore(self.cs.FILLER_STORE.root, control.PROGRAM)
        payload = self.payload()
        payload["profile"] = dict(BASE)
        legacy_store.prepare("legacy", payload, "local-test")
        before = legacy_store.public_state("legacy")
        self.assertEqual(self.start(version="legacy")[0], 409)
        self.assertFalse((self.cs.CONTROL_DIR / "active-filler.version").exists())
        self.assertEqual(legacy_store.public_state("legacy"), before)
