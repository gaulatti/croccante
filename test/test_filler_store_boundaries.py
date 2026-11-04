#!/usr/bin/env python3
"""Validation, atomic-publication and failure boundaries of filler preparation.

These tests replace only the network and ffmpeg process boundaries; manifest
handling, profile validation, checksum verification, quarantine/rollback and
cleanup run as production code.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import filler_store
from filler_store import FillerStore, PreparationError, Profile

PROFILE = {
    "width": 160, "height": 90, "fps": 10, "videoBitrate": "200k",
    "audioRate": 44100, "audioChannels": 2, "audioBitrate": "64k",
    "gop": 10, "loopSeconds": 2,
}
SOURCE_BYTES = b"fictional-source-bytes"
SOURCE_SHA = hashlib.sha256(SOURCE_BYTES).hexdigest()
OUTPUT_BYTES = b"fictional-prepared-flv"
VALID_OUTPUT_PROBE = {
    "streams": [
        {"codec_type": "video", "codec_name": "h264", "width": 160, "height": 90, "avg_frame_rate": "10/1", "pix_fmt": "yuv420p"},
        {"codec_type": "audio", "codec_name": "aac", "sample_rate": "44100", "channels": 2},
    ]
}


def reason_of(call, *args, **kwargs) -> str:
    try:
        call(*args, **kwargs)
    except PreparationError as error:
        return error.reason
    raise AssertionError("expected a PreparationError")


def request(command: str = "command-a", **source: object) -> dict[str, object]:
    return {
        "commandId": command,
        "source": {"id": "source-a", "sha256": SOURCE_SHA, "downloadUrl": "https://media.example/source", **source},
        "profile": dict(PROFILE),
    }


class ProfileValidationTest(unittest.TestCase):
    def test_accepts_the_documented_profile(self) -> None:
        profile = Profile.parse(PROFILE)
        self.assertEqual(profile.public(), PROFILE)
        self.assertEqual(Profile.parse({k: v for k, v in PROFILE.items() if k != "loopSeconds"}).loop_seconds, 10)

    def test_every_invalid_profile_is_rejected(self) -> None:
        cases = {
            "not-an-object": "x",
            "missing-field": {k: v for k, v in PROFILE.items() if k != "fps"},
            "non-numeric": {**PROFILE, "width": "wide"},
            "null": {**PROFILE, "height": None},
            "width-small": {**PROFILE, "width": 14},
            "height-large": {**PROFILE, "height": 4322},
            "odd-width": {**PROFILE, "width": 161},
            "odd-height": {**PROFILE, "height": 91},
            "fps-zero": {**PROFILE, "fps": 0},
            "gop-large": {**PROFILE, "gop": 601},
            "audio-rate-low": {**PROFILE, "audioRate": 7999},
            "audio-channels": {**PROFILE, "audioChannels": 3},
            "loop-zero": {**PROFILE, "loopSeconds": 0},
            "loop-large": {**PROFILE, "loopSeconds": 301},
            "video-bitrate": {**PROFILE, "videoBitrate": "fast"},
            "audio-bitrate": {**PROFILE, "audioBitrate": "0k"},
        }
        for name, raw in cases.items():
            with self.subTest(case=name):
                self.assertEqual(reason_of(Profile.parse, raw), "invalid-profile")


class PreparationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = FillerStore(Path(self.temporary.name), "program-a")
        self.downloads: list[str] = []

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def prepare(self, version: str = "v1", payload: dict[str, object] | None = None, *, probes=None, transcode=None):
        probe_results = iter(probes if probes is not None else [{"streams": [{"codec_type": "video", "codec_name": "h264"}]}, VALID_OUTPUT_PROBE])

        def download(url: str, destination: Path) -> None:
            self.downloads.append(url)
            destination.write_bytes(SOURCE_BYTES)

        def write_output(source: Path, output: Path, profile: Profile, probe: dict) -> None:
            output.write_bytes(OUTPUT_BYTES)

        with (
            patch.object(FillerStore, "_download", staticmethod(download)),
            patch.object(FillerStore, "_probe", staticmethod(lambda path: next(probe_results))),
            patch.object(FillerStore, "_transcode", staticmethod(transcode or write_output)),
        ):
            return self.store.prepare(version, payload if payload is not None else request(), "2026-10-28T00:00:00Z")


class RequestValidationTest(PreparationFixture):
    def test_invalid_versions_and_sources_are_refused_before_any_download(self) -> None:
        self.assertEqual(reason_of(self.store.version_dir, "../escape"), "invalid-version")
        cases = {
            "source-type": ("invalid-source", {**request(), "source": "x"}),
            "source-id": ("invalid-source", request(id="-bad")),
            "source-checksum": ("invalid-source", request(sha256="abc")),
            "download-scheme": ("invalid-download", request(downloadUrl="ftp://media.example/source")),
        }
        for name, (reason, payload) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(reason_of(self.prepare, "v1", payload), reason)
        self.assertEqual(self.downloads, [])

    def test_profile_errors_surface_through_preparation(self) -> None:
        payload = {**request(), "profile": {**PROFILE, "fps": 0}}
        self.assertEqual(reason_of(self.prepare, "v1", payload), "invalid-profile")


class ManifestIntegrityTest(PreparationFixture):
    def test_unreadable_manifests_and_missing_or_altered_assets_are_unprepared(self) -> None:
        self.assertIsNone(self.store.manifest("absent"))
        state = self.store.public_state("absent")
        self.assertEqual(state, {"version": "absent", "status": "unprepared", "ready": False})

        self.prepare("v1")
        asset = self.store.version_dir("v1") / "filler.flv"
        self.assertTrue(self.store.public_state("v1")["ready"])
        asset.write_bytes(b"tampered")
        self.assertIsNone(self.store.manifest("v1"))
        asset.unlink()
        self.assertIsNone(self.store.manifest("v1"))

    def test_corrupt_manifest_is_unprepared(self) -> None:
        directory = self.store.version_dir("v1")
        directory.mkdir()
        (directory / "manifest.json").write_text("{", encoding="utf-8")
        self.assertIsNone(self.store.manifest("v1"))


class AtomicPublicationTest(PreparationFixture):
    def test_invalid_existing_directory_is_replaced_and_quarantine_removed(self) -> None:
        target = self.store.version_dir("v1")
        target.mkdir()
        (target / "stale").write_text("left behind", encoding="utf-8")
        result = self.prepare("v1")
        self.assertTrue(result["ready"])
        self.assertTrue((target / "filler.flv").is_file())
        self.assertFalse((target / "stale").exists())
        self.assertEqual([p.name for p in self.store.program_root.iterdir() if p.name.startswith(".")], [])

    def test_failed_publication_restores_the_previous_directory(self) -> None:
        target = self.store.version_dir("v1")
        target.mkdir()
        (target / "stale").write_text("kept", encoding="utf-8")
        real_rename = os.rename

        def fail_publication(source: object, destination: object) -> None:
            if Path(destination) == target and ".invalid." not in Path(source).name:
                raise OSError("injected rename failure")
            real_rename(source, destination)  # type: ignore[arg-type]

        with patch.object(filler_store.os, "rename", fail_publication):
            with self.assertRaises(OSError):
                self.prepare("v1")
        self.assertEqual((target / "stale").read_text(encoding="utf-8"), "kept")
        self.assertEqual([p.name for p in self.store.program_root.iterdir() if p.name.startswith(".")], [])

    def test_failed_publication_without_prior_directory_leaves_nothing(self) -> None:
        with patch.object(filler_store.os, "rename", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.prepare("v1")
        self.assertFalse(self.store.version_dir("v1").exists())

    def race(self, rewrite_request_hash: bool):
        target = self.store.version_dir("v1")
        real_rename = os.rename

        def lose_race(source: object, destination: object) -> None:
            if Path(destination) == target:
                shutil.copytree(source, target)  # type: ignore[arg-type]
                if rewrite_request_hash:
                    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
                    manifest["requestHash"] = "0" * 64
                    (target / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                raise FileExistsError(str(target))
            real_rename(source, destination)  # type: ignore[arg-type]

        return patch.object(filler_store.os, "rename", lose_race)

    def test_concurrent_identical_preparation_is_idempotent(self) -> None:
        with self.race(rewrite_request_hash=False):
            self.assertTrue(self.prepare("v1")["ready"])

    def test_concurrent_conflicting_preparation_is_a_version_conflict(self) -> None:
        with self.race(rewrite_request_hash=True):
            self.assertEqual(reason_of(self.prepare, "v1"), "version-conflict")

    def test_failed_stages_clean_their_staging_directory(self) -> None:
        def broken_transcode(source: Path, output: Path, profile: Profile, probe: dict) -> None:
            raise PreparationError("transcode-failed")

        self.assertEqual(reason_of(self.prepare, "v1", transcode=broken_transcode), "transcode-failed")
        self.assertEqual(list(self.store.program_root.iterdir()), [])

    def test_checksum_mismatch_is_rejected(self) -> None:
        self.assertEqual(reason_of(self.prepare, "v1", request(sha256="0" * 64)), "checksum-mismatch")

    def test_output_that_misses_the_profile_is_rejected(self) -> None:
        wrong = {"streams": [{"codec_type": "video", "codec_name": "vp9"}]}
        probes = [VALID_OUTPUT_PROBE, wrong]
        self.assertEqual(reason_of(self.prepare, "v1", probes=probes), "profile-mismatch")


class CleanupTest(PreparationFixture):
    def test_cleanup_ignores_hidden_files_and_unprepared_directories(self) -> None:
        for name in ("v1", "v2"):
            self.prepare(name, request(f"command-{name}"))
        (self.store.program_root / ".staging").mkdir()
        (self.store.program_root / "notes.txt").write_text("not a version", encoding="utf-8")
        (self.store.program_root / "unprepared").mkdir()
        self.store.cleanup(None, retain=1)
        ready = [name for name in ("v1", "v2") if self.store.public_state(name)["ready"]]
        self.assertEqual(len(ready), 1)
        self.assertTrue((self.store.program_root / ".staging").is_dir())
        self.assertTrue((self.store.program_root / "notes.txt").is_file())
        self.assertTrue((self.store.program_root / "unprepared").is_dir())


class FakeResponse:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self, _size: int) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


class DownloadTest(unittest.TestCase):
    def test_streams_a_bounded_download_to_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "source"
            with patch.object(filler_store.urllib.request, "urlopen", return_value=FakeResponse([b"ab", b"cd"])):
                FillerStore._download("https://media.example/source", destination)
            self.assertEqual(destination.read_bytes(), b"abcd")

    def test_oversized_downloads_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(filler_store, "MAX_DOWNLOAD_BYTES", 3):
                with patch.object(filler_store.urllib.request, "urlopen", return_value=FakeResponse([b"ab", b"cd"])):
                    self.assertEqual(
                        reason_of(FillerStore._download, "https://media.example/source", Path(directory) / "source"),
                        "download-too-large",
                    )

    def test_transport_failures_never_leak_their_detail(self) -> None:
        class Closeable(Exception):
            closed = False

            def close(self) -> None:
                self.closed = True

        closeable = Closeable("signed-url-detail")
        for failure in (ValueError("signed-url-detail"), closeable):
            with self.subTest(failure=type(failure).__name__), tempfile.TemporaryDirectory() as directory:
                with patch.object(filler_store.urllib.request, "urlopen", side_effect=failure):
                    self.assertEqual(
                        reason_of(FillerStore._download, "https://media.example/source", Path(directory) / "source"),
                        "download-failed",
                    )
        self.assertTrue(closeable.closed)


class FfmpegBoundaryTest(unittest.TestCase):
    def test_probe_returns_parsed_streams(self) -> None:
        completed = subprocess.CompletedProcess([], 0, stdout='{"streams": []}')
        with patch.object(filler_store.subprocess, "run", return_value=completed) as run:
            self.assertEqual(FillerStore._probe(Path("input")), {"streams": []})
        self.assertEqual(run.call_args.args[0][0], "ffprobe")

    def test_probe_failures_are_reported_without_detail(self) -> None:
        failures = (
            subprocess.CalledProcessError(1, "ffprobe"),
            OSError("ffprobe missing"),
            subprocess.TimeoutExpired("ffprobe", 30),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                with patch.object(filler_store.subprocess, "run", side_effect=failure):
                    self.assertEqual(reason_of(FillerStore._probe, Path("input")), "probe-failed")
        with patch.object(filler_store.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="not json")):
            self.assertEqual(reason_of(FillerStore._probe, Path("input")), "probe-failed")

    def test_transcode_requires_a_video_stream(self) -> None:
        profile = Profile.parse(PROFILE)
        with patch.object(filler_store.subprocess, "run") as run:
            self.assertEqual(reason_of(FillerStore._transcode, Path("in"), Path("out"), profile, {"streams": [{"codec_type": "audio"}]}), "missing-video")
        run.assert_not_called()

    def test_transcode_failures_are_reported_without_detail(self) -> None:
        profile = Profile.parse(PROFILE)
        probe = {"streams": [{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio"}]}
        for failure in (subprocess.CalledProcessError(1, "ffmpeg"), OSError("ffmpeg missing")):
            with self.subTest(failure=type(failure).__name__):
                with patch.object(filler_store.subprocess, "run", side_effect=failure):
                    self.assertEqual(reason_of(FillerStore._transcode, Path("in"), Path("out"), profile, probe), "transcode-failed")

    def test_transcode_commands_match_the_source_kind(self) -> None:
        profile = Profile.parse({**PROFILE, "audioChannels": 1})
        commands = {}
        for name, probe in (
            ("still-without-audio", {"streams": [{"codec_type": "video", "codec_name": "png"}]}),
            ("video-with-audio", {"streams": [{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio"}]}),
        ):
            with patch.object(filler_store.subprocess, "run") as run:
                FillerStore._transcode(Path("in"), Path("out.flv"), profile, probe)
            commands[name] = run.call_args.args[0]
        still, video = commands["still-without-audio"], commands["video-with-audio"]
        self.assertIn("-loop", still)
        self.assertIn("anullsrc=sample_rate=44100:channel_layout=mono", still)
        self.assertIn("1:a:0", still)
        self.assertIn("-stream_loop", video)
        self.assertNotIn("anullsrc=sample_rate=44100:channel_layout=mono", video)
        self.assertIn("0:a:0", video)
        stereo = Profile.parse(PROFILE)
        with patch.object(filler_store.subprocess, "run") as run:
            FillerStore._transcode(Path("in"), Path("out.flv"), stereo, {"streams": [{"codec_type": "video", "codec_name": "h264"}]})
        self.assertIn("anullsrc=sample_rate=44100:channel_layout=stereo", run.call_args.args[0])


class OutputValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = Profile.parse(PROFILE)

    def variant(self, *, video: dict | None = None, audio: dict | None = None, drop: str | None = None) -> dict:
        streams = json.loads(json.dumps(VALID_OUTPUT_PROBE["streams"]))
        streams[0].update(video or {})
        streams[1].update(audio or {})
        if drop:
            streams = [item for item in streams if item["codec_type"] != drop]
        return {"streams": streams}

    def test_accepts_exactly_the_requested_profile(self) -> None:
        FillerStore._validate_output(VALID_OUTPUT_PROBE, self.profile)

    def test_every_deviation_is_a_profile_mismatch(self) -> None:
        cases = {
            "no-video": self.variant(drop="video"),
            "no-audio": self.variant(drop="audio"),
            "video-codec": self.variant(video={"codec_name": "vp9"}),
            "audio-codec": self.variant(audio={"codec_name": "opus"}),
            "width": self.variant(video={"width": 320}),
            "height": self.variant(video={"height": 360}),
            "frame-rate": self.variant(video={"avg_frame_rate": "30/1"}),
            "frame-rate-unparseable": self.variant(video={"avg_frame_rate": "fast"}),
            "frame-rate-zero-denominator": self.variant(video={"avg_frame_rate": "1/0"}),
            "pixel-format": self.variant(video={"pix_fmt": "yuv444p"}),
            "sample-rate": self.variant(audio={"sample_rate": "48000"}),
            "channels": self.variant(audio={"channels": 1}),
        }
        for name, probe in cases.items():
            with self.subTest(case=name):
                self.assertEqual(reason_of(FillerStore._validate_output, probe, self.profile), "profile-mismatch")


if __name__ == "__main__":
    unittest.main()
