#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import as_aired_capture as capture
from as_aired_capture import (
    AsAiredCapture,
    AsAiredSession,
    CaptureConfigError,
    CaptureSettings,
    apply_retention,
    build_capture_command,
    build_probe_command,
    derive_source_timeline,
    parse_probe,
    usage,
)
from recording_manifest import RecordingContractError, load_manifest, validate_manifest

START = datetime(2026, 9, 7, 0, 0, 0, tzinfo=timezone.utc)
HASH = "0" * 64
VIDEO = {"codec": "h264", "width": 1280, "height": 720, "frameRate": {"numerator": 30, "denominator": 1}}
AUDIO = {"codec": "aac", "sampleRateHz": 48000, "channels": 2}
SECRET_URL = "rtmps://example.invalid/live/SENTINEL-STREAM-KEY"


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: int) -> None:
        self.now += timedelta(**delta)


class FakeProcess:
    def __init__(self) -> None:
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 1

    def terminate(self) -> None:
        self.terminated = True
        self.alive = False

    def wait(self, timeout: int) -> None:
        self.waited = timeout


def probe(path: Path) -> dict:
    return {"durationMs": 10_000, "video": VIDEO, "audio": AUDIO}


def raw(**overrides) -> dict:
    value = {
        "enabled": True, "source": "rtmp://127.0.0.1/live/program", "segmentSeconds": 10,
        "maxSessionBytes": 10_000, "maxTotalBytes": 20_000, "retentionDays": 7,
        "classification": "public-broadcast", "video": VIDEO, "audio": AUDIO,
    }
    value.update(overrides)
    return value


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.clock = Clock()
        self.settings = CaptureSettings.from_mapping(raw(), root=self.root)
        self.processes: list[FakeProcess] = []

    def start(self, command):
        self.commands = getattr(self, "commands", []) + [command]
        process = FakeProcess()
        self.processes.append(process)
        return process

    def facade(self, **kwargs) -> AsAiredCapture:
        args = {"destination_count": 2, "configuration_hash": HASH, "clock": self.clock, "start": self.start, "probe": probe}
        args.update(kwargs)
        return AsAiredCapture(self.settings, **args)

    def segment(self, session_dir: Path, number: int, size: int = 100) -> Path:
        path = session_dir / "artifacts" / f"segment-{number:06d}.ts"
        path.write_bytes(bytes([number]) * size)
        self.clock.advance(seconds=10)  # a 10 s segment takes 10 s of wall time to air
        return path

    def manifest(self, cap: AsAiredCapture) -> dict:
        return json.loads((cap.session.directory / "manifest.json").read_text())


class SettingsTests(Base):
    def test_capture_is_off_unless_explicitly_enabled(self) -> None:
        self.assertIsNone(CaptureSettings.from_mapping(None, root=self.root))
        self.assertIsNone(CaptureSettings.from_mapping({"enabled": False}, root=self.root))
        self.assertIsNone(CaptureSettings.from_mapping({"enabled": "true"}, root=self.root))

    def test_valid_settings_and_every_rejection(self) -> None:
        self.assertEqual(self.settings.segment_seconds, 10)
        self.assertIsNotNone(CaptureSettings.from_mapping(raw(source="/run/relay/program.ts", legalHold=True), root=self.root))
        for source in ("rtmp://localhost/live/x", "rtmp://[::1]/live/x"):
            self.assertIsNotNone(CaptureSettings.from_mapping(raw(source=source), root=self.root))
        bad = [
            {"unknown": 1}, {"segmentSeconds": 1}, {"segmentSeconds": True}, {"segmentSeconds": "10"},
            {"maxSessionBytes": 0}, {"retentionDays": 366}, {"source": "rtmps://example.invalid/x"},
            {"source": 5}, {"classification": "secret"}, {"classification": None}, {"legalHold": "yes"}, {"video": None, "audio": None}, {"video": "h264"},
            {"maxSessionBytes": 30_000},
            {"source": "rtmp://127.0.0.1.evil.example/live"}, {"source": "rtmp://user:pw@127.0.0.1/x"},
            {"source": "rtmp://localhost@evil.example/x"}, {"source": "/tmp/a://b"}, {"source": "http://127.0.0.1/x"},
        ]
        for override in bad:
            with self.subTest(override=override), self.assertRaises(CaptureConfigError):
                CaptureSettings.from_mapping(raw(**override), root=self.root)
        broken = raw()
        del broken["retentionDays"]
        with self.assertRaises(CaptureConfigError):
            CaptureSettings.from_mapping(broken, root=self.root)

    def test_command_builders_copy_streams_and_never_contain_destinations(self) -> None:
        command = build_capture_command(self.settings, self.root / "s", 3)
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("copy", command)
        self.assertEqual(command[command.index("-segment_start_number") + 1], "3")
        self.assertTrue(command[-1].endswith("artifacts/segment-%06d.ts"))
        self.assertNotIn(SECRET_URL, " ".join(command))
        self.assertEqual(build_probe_command(Path("x.ts"))[0], "ffprobe")

    def test_probe_parser_reduces_ffprobe_to_contract_facts(self) -> None:
        output = json.dumps({
            "format": {"duration": "10.004"},
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1280, "height": 720, "avg_frame_rate": "30000/1001"},
                {"codec_type": "video", "codec_name": "ignored", "width": 1, "height": 1, "avg_frame_rate": "1/1"},
                {"codec_type": "audio", "codec_name": "aac", "sample_rate": "48000", "channels": 2},
                {"codec_type": "audio", "codec_name": "ignored", "sample_rate": "8000", "channels": 1},
                {"codec_type": "data"},
            ],
        })
        facts = parse_probe(output)
        self.assertEqual(facts["durationMs"], 10004)
        self.assertEqual(facts["video"]["frameRate"], {"numerator": 30000, "denominator": 1001})
        self.assertEqual(facts["audio"]["codec"], "aac")

    def test_default_process_boundaries_use_the_documented_commands(self) -> None:
        completed = mock.Mock(stdout=json.dumps({"format": {"duration": "1"}, "streams": []}))
        with mock.patch.object(subprocess, "run", return_value=completed) as run:
            self.assertEqual(capture.default_probe(Path("a.ts"))["durationMs"], 1000)
        self.assertEqual(run.call_args.args[0], build_probe_command(Path("a.ts")))
        with mock.patch.object(subprocess, "Popen") as popen:
            capture.default_start(["ffmpeg"])
        self.assertEqual(popen.call_args.args[0], ["ffmpeg"])
        self.assertEqual(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)


class TimelineTests(unittest.TestCase):
    def test_gaps_are_unobserved_and_uncovered_modes_do_not_leak(self) -> None:
        segments = [
            {"startedOffsetMs": 0, "durationMs": 10_000},
            {"startedOffsetMs": 20_000, "durationMs": 10_000},
        ]
        noted = [(0, "unobserved"), (0, "live"), (12_000, "filler"), (25_000, "live")]
        timeline = derive_source_timeline(noted, segments, 30_000)
        self.assertEqual(
            [(e["startedOffsetMs"], e["mode"]) for e in timeline],
            [(0, "live"), (10_000, "unobserved"), (20_000, "filler"), (25_000, "live")],
        )
        self.assertEqual(derive_source_timeline([], [], 0), [])
        self.assertEqual(derive_source_timeline([(0, "live")], [], 5_000)[0]["mode"], "unobserved")


class SessionTests(Base):
    def test_clean_session_is_complete_with_visible_filler_and_a_verified_report(self) -> None:
        cap = self.facade()
        self.assertEqual(cap.session.state, "recording")
        self.assertIn("-segment_start_number", self.commands[0])
        cap.note_source("live")
        cap.destination_started(1)
        cap.destination_started(2)
        self.segment(cap.session.directory, 1)
        self.segment(cap.session.directory, 2)
        cap.note_source("filler")
        cap.note_source("filler")
        cap.poll()
        cap.destination_ended(1, "accepted", 5000, None)
        cap.destination_ended(2, "accepted", 4000, None)
        self.segment(cap.session.directory, 3, 50)
        cap.close()

        manifest = load_manifest(cap.session.directory / "manifest.json", artifact_root=cap.session.directory)
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual(len(manifest["capture"]["segments"]), 3)
        self.assertEqual(manifest["capture"]["finalArtifact"]["byteLength"], 250)
        modes = [event["mode"] for event in manifest["capture"]["sourceTimeline"]]
        self.assertEqual(modes, ["live", "filler"])
        report = (cap.session.directory / "report.txt").read_text()
        self.assertIn("accepted:2", report)
        self.assertIn("filler_ms:", report)
        self.assertTrue(self.processes[0].terminated)
        self.assertEqual(cap.snapshot(), {"state": "complete", "segments": 3, "bytes": 250, "recorder_failures": 0})
        # A closed session ignores late relay callbacks without error or failure.
        cap.note_source("live"), cap.destination_started(1), cap.destination_ended(1, "failed", 0, "destination_unavailable")
        cap.poll(), cap.recover(), cap.close()
        self.assertEqual(cap.failures, 0)

    def test_reports_distinguish_failed_unknown_and_not_attempted_delivery(self) -> None:
        cap = self.facade(destination_count=3)
        cap.destination_started(1)
        cap.destination_started(2)
        self.clock.advance(seconds=1)
        cap.destination_ended(1, "failed", 0, "destination_rejected")
        cap.destination_started(1)
        self.segment(cap.session.directory, 1)
        cap.close()
        manifest = self.manifest(cap)
        self.assertEqual(manifest["state"], "partial")
        statuses = [item["finalStatus"] for item in manifest["destinations"]]
        self.assertEqual(statuses, ["unknown", "unknown", "not_attempted"])
        report = (cap.session.directory / "report.txt").read_text()
        self.assertIn("unknown:2", report)
        self.assertIn("not_attempted:1", report)

    def test_unattempted_destination_alone_makes_the_session_partial(self) -> None:
        cap = self.facade()
        self.segment(cap.session.directory, 1)
        cap.close()
        manifest = self.manifest(cap)
        self.assertEqual((manifest["state"], manifest["transitions"][-1]["reasonCode"]), ("partial", "destination_unavailable"))

    def test_failed_destination_attempt_is_partial_not_complete(self) -> None:
        cap = self.facade()
        cap.destination_started(1)
        cap.destination_started(2)
        self.clock.advance(seconds=1)
        cap.destination_ended(1, "accepted", 10, None)
        cap.destination_ended(2, "failed", 0, "destination_unavailable")
        self.segment(cap.session.directory, 1)
        cap.close()
        self.assertEqual(self.manifest(cap)["state"], "partial")

    def test_crash_is_a_visible_gap_and_recovery_resumes_numbering(self) -> None:
        cap = self.facade(destination_count=1)
        cap.destination_started(1)
        self.segment(cap.session.directory, 1)
        self.segment(cap.session.directory, 2)
        cap.poll()  # segment 1 is final; segment 2 may still be written
        self.assertEqual(self.manifest(cap)["capture"]["capturedDurationMs"], 10_000)
        self.processes[0].alive = False
        cap.poll()  # recorder died: segment 2 is complete, state is interrupted
        self.assertEqual(self.manifest(cap)["state"], "interrupted")
        cap.poll()
        self.clock.advance(seconds=5)
        cap.recover()
        self.assertEqual(self.commands[1][self.commands[1].index("-segment_start_number") + 1], "3")
        self.segment(cap.session.directory, 3)
        self.clock.advance(seconds=10)
        cap.destination_ended(1, "accepted", 10, None)
        cap.close()
        manifest = load_manifest(cap.session.directory / "manifest.json", artifact_root=cap.session.directory)
        self.assertEqual(manifest["state"], "partial")
        self.assertIsNone(manifest["capture"]["finalArtifact"])
        modes = [event["mode"] for event in manifest["capture"]["sourceTimeline"]]
        self.assertIn("unobserved", modes)
        self.assertEqual(manifest["capture"]["segments"][2]["startedOffsetMs"], 25_000)

    def test_recover_is_a_no_op_unless_interrupted(self) -> None:
        cap = self.facade()
        cap.recover()
        self.assertEqual(len(self.commands), 1)

    def test_session_with_no_segments_is_partial_and_valid(self) -> None:
        cap = self.facade()
        cap.close()
        manifest = self.manifest(cap)
        self.assertEqual((manifest["state"], manifest["capture"]["elapsedDurationMs"]), ("partial", 0))

    def test_interruption_without_recovery_still_finalizes(self) -> None:
        cap = self.facade()
        self.segment(cap.session.directory, 1)
        self.processes[0].alive = False
        cap.poll()
        cap.close()
        self.assertEqual(self.manifest(cap)["state"], "partial")
        self.assertFalse(self.processes[0].terminated)

    def test_per_session_quota_stops_capture_and_discards_the_oversized_segment(self) -> None:
        self.settings = CaptureSettings.from_mapping(raw(maxSessionBytes=150), root=self.root)
        cap = self.facade()
        self.segment(cap.session.directory, 1, 100)
        self.segment(cap.session.directory, 2, 100)
        self.segment(cap.session.directory, 3, 100)
        cap.poll()
        self.assertTrue(self.processes[0].terminated)
        cap.close()
        manifest = self.manifest(cap)
        self.assertEqual(len(manifest["capture"]["segments"]), 1)
        self.assertEqual((manifest["state"], manifest["transitions"][-1]["reasonCode"]), ("partial", "disk_full"))
        self.assertFalse((cap.session.directory / "artifacts" / "segment-000002.ts").exists())

    def test_codec_change_is_rejected_and_the_session_is_partial(self) -> None:
        facts = {"video": VIDEO, "audio": AUDIO}

        def changing(_path):
            return {"durationMs": 10_000, **facts}

        cap = self.facade(probe=changing)
        self.segment(cap.session.directory, 1)
        self.segment(cap.session.directory, 2)
        self.segment(cap.session.directory, 3)
        facts["audio"] = {**AUDIO, "codec": "mp3"}
        cap.poll()  # the changed audio codec fails verification of the first segment
        cap.close()
        manifest = self.manifest(cap)
        self.assertEqual(manifest["state"], "partial")
        self.assertEqual(manifest["transitions"][-1]["reasonCode"], "capture_interrupted")
        self.assertFalse((cap.session.directory / "artifacts" / "segment-000001.ts").exists())

    def test_total_quota_blocks_a_new_session_before_it_records(self) -> None:
        self.settings = CaptureSettings.from_mapping(raw(maxSessionBytes=50, maxTotalBytes=100), root=self.root)
        older = self.facade(session_id="22222222-2222-4222-8222-222222222222")
        older.session.directory.joinpath("artifacts", "segment-000001.ts").write_bytes(b"x" * 500)
        blocked = self.facade(session_id="33333333-3333-4333-8333-333333333333")
        self.assertEqual(self.manifest(blocked)["state"], "failed")
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(blocked.snapshot()["state"], "failed")

    def test_invalid_session_inputs_are_rejected(self) -> None:
        with self.assertRaises(CaptureConfigError):
            AsAiredSession(self.settings, destination_count=0, configuration_hash=HASH, clock=self.clock)
        session = AsAiredSession(self.settings, destination_count=1, configuration_hash=HASH, clock=self.clock)
        session.start_recording()
        with self.assertRaises(CaptureConfigError):
            session.note_source("sideways")
        with self.assertRaises(CaptureConfigError):
            session.note_source("unobserved")
        self.assertEqual(session.state, "recording")

    def test_saturating_twice_records_one_error(self) -> None:
        session = AsAiredSession(self.settings, destination_count=1, configuration_hash=HASH, clock=self.clock)
        session.saturate()
        session.saturate()
        self.assertEqual([e["code"] for e in session.manifest()["errors"]], ["capture_interrupted"])

    def test_final_artifact_verification_failure_is_reported_as_partial(self) -> None:
        cap = self.facade(destination_count=1)
        cap.destination_started(1)
        self.clock.advance(seconds=1)
        cap.destination_ended(1, "accepted", 10, None)
        self.segment(cap.session.directory, 1)
        with mock.patch.object(capture.AsAiredSession, "_concatenate", side_effect=OSError("disk")):
            cap.close()
        manifest = self.manifest(cap)
        self.assertEqual((manifest["state"], manifest["transitions"][-1]["reasonCode"]), ("partial", "finalization_failed"))
        self.assertIsNone(manifest["capture"]["finalArtifact"])

    def test_corrupted_final_artifact_fails_verification(self) -> None:
        cap = self.facade(destination_count=1)
        cap.destination_started(1)
        self.clock.advance(seconds=1)
        cap.destination_ended(1, "accepted", 10, None)
        self.segment(cap.session.directory, 1)
        real_sha = capture._sha256
        with mock.patch.object(capture, "_sha256", side_effect=lambda path: "f" * 64 if path.name == "final.ts" else real_sha(path)):
            cap.close()
        self.assertEqual(self.manifest(cap)["transitions"][-1]["reasonCode"], "finalization_failed")


class IsolationTests(Base):
    def test_start_failure_never_raises_and_is_counted(self) -> None:
        def broken(_command):
            raise OSError("no ffmpeg")

        cap = self.facade(start=broken)
        self.assertEqual(cap.failures, 1)
        cap.close()  # session is still open in `recording`; closing must be safe
        self.assertEqual(self.manifest(cap)["state"], "partial")

    def test_open_failure_leaves_no_session_and_every_call_is_inert(self) -> None:
        self.settings = CaptureSettings.from_mapping(raw(), root=self.root / "missing" / "deep")
        with mock.patch.object(capture.Path, "mkdir", side_effect=OSError("readonly")):
            cap = self.facade()
        self.assertEqual((cap.failures, cap.session), (1, None))
        cap.note_source("live"), cap.destination_started(1), cap.destination_ended(1, "failed", 0, "x"), cap.poll(), cap.recover(), cap.close()
        self.assertEqual(cap.snapshot(), {"state": "unavailable", "segments": 0, "bytes": 0, "recorder_failures": 1})

    def test_unprobeable_segment_becomes_a_visible_gap_and_numbering_stays_contiguous(self) -> None:
        def flaky(path):
            if path.name == "segment-000002.ts":
                raise subprocess.CalledProcessError(1, "ffprobe")
            return probe(path)

        cap = self.facade(probe=flaky, destination_count=1)
        for number in (1, 2, 3, 4):
            self.segment(cap.session.directory, number)
        cap.poll()
        self.assertEqual(cap.failures, 0)
        cap.close()
        manifest = load_manifest(cap.session.directory / "manifest.json", artifact_root=cap.session.directory)
        self.assertEqual(manifest["state"], "partial")
        self.assertEqual([s["storageKey"] for s in manifest["capture"]["segments"]], ["artifacts/segment-000001.ts", "artifacts/segment-000002.ts", "artifacts/segment-000003.ts"])
        self.assertEqual([s["startedOffsetMs"] for s in manifest["capture"]["segments"]], [0, 20_000, 30_000])
        self.assertIn("unobserved", [e["mode"] for e in manifest["capture"]["sourceTimeline"]])
        self.assertIsNone(manifest["capture"]["finalArtifact"])

    def test_close_falls_back_to_a_failed_manifest_and_never_raises(self) -> None:
        cap = self.facade()
        self.segment(cap.session.directory, 1)
        with mock.patch.object(capture.AsAiredSession, "finalize", side_effect=RuntimeError("boom")):
            cap.close()
        self.assertEqual(self.manifest(cap)["state"], "failed")
        self.assertEqual(cap.failures, 1)
        other = self.facade(session_id="55555555-5555-4555-8555-555555555555")
        with mock.patch.object(capture.AsAiredSession, "persist", side_effect=OSError("disk")):
            other.close()
        self.assertEqual(other.failures, 2)

    def test_retention_failure_does_not_prevent_capture(self) -> None:
        with mock.patch.object(capture, "apply_retention", side_effect=OSError("busy")):
            cap = self.facade()
        self.assertEqual(cap.failures, 1)
        self.assertEqual(cap.session.state, "recording")

    def test_evidence_bounds_close_the_session_validly_instead_of_freezing_it(self) -> None:
        cap = self.facade(destination_count=2)
        for _ in range(40):
            self.clock.advance(seconds=1)
            cap.destination_started(1)
            cap.destination_ended(1, "failed", 0, "destination_unavailable")
        manifest = load_manifest(cap.session.directory / "manifest.json")
        self.assertEqual(manifest["state"], "partial")
        self.assertEqual(manifest["transitions"][-1]["reasonCode"], "capture_interrupted")
        self.assertEqual(cap.failures, 0)
        self.assertLessEqual(len(manifest["errors"]), 64)
        cap.destination_started(1)  # a closed session ignores later relay callbacks

    def test_crash_loops_and_source_flapping_also_stay_within_contract_bounds(self) -> None:
        cap = self.facade(destination_count=1)
        for number in range(1, 40):
            self.segment(cap.session.directory, number)
            self.processes[-1].alive = False
            cap.poll()
            self.clock.advance(seconds=1)
            cap.recover()
            if cap.session.terminal:
                break
        self.assertTrue(cap.session.terminal)
        self.assertEqual(self.manifest(cap)["state"], "partial")
        self.assertEqual(cap.failures, 0)

        flapping = self.facade(session_id="66666666-6666-4666-8666-666666666666")
        for index in range(100):
            if flapping.session.terminal:
                break
            self.clock.advance(seconds=1)
            flapping.note_source("filler" if index % 2 else "live")
        self.assertEqual(self.manifest(flapping)["state"], "partial")
        self.assertEqual(flapping.failures, 0)

        gappy = self.facade(session_id="77777777-7777-4777-8777-777777777777", probe=lambda _p: {"durationMs": 1_000, "video": VIDEO, "audio": AUDIO})
        number = 0
        while not gappy.session.terminal and number < 200:
            number += 1
            self.segment(gappy.session.directory, number)
            gappy.poll()
            gappy.note_source("filler" if number % 2 else "live")
        self.assertTrue(gappy.session.terminal)
        self.assertEqual(gappy.failures, 0)

    def test_relay_calls_with_bad_arguments_are_isolated_and_leave_state_valid(self) -> None:
        cap = self.facade()
        cap.note_source("sideways")
        cap.destination_started(99)
        cap.destination_started(True)
        cap.destination_ended(1, "accepted", 5, None)  # nothing in progress
        cap.destination_started(1)
        cap.destination_started(1)  # already in progress
        for status, observed, code in (("accepted", 0, None), ("in_progress", 0, None), ("accepted", True, None), ("failed", -1, "destination_unavailable"), ("failed", 0, "made_up"), ("bogus", 0, None)):
            cap.destination_ended(1, status, observed, code)
        self.assertEqual(cap.failures, 11)
        # Refused evidence never entered state, so the session still closes validly.
        cap.destination_ended(1, "unknown", 0, None)
        self.segment(cap.session.directory, 1)
        cap.close()
        manifest = load_manifest(cap.session.directory / "manifest.json", artifact_root=cap.session.directory)
        self.assertEqual(manifest["destinations"][0]["attempts"][0]["errorCode"], "destination_unavailable")
        self.assertEqual(cap.failures, 11)

    def test_many_unprobeable_segments_in_one_poll_stay_within_the_error_bound(self) -> None:
        def broken(_path):
            raise OSError("ffprobe unavailable")

        cap = self.facade(probe=broken, destination_count=1)
        for number in range(1, 80):
            self.segment(cap.session.directory, number)
        cap.poll()
        manifest = load_manifest(cap.session.directory / "manifest.json")
        self.assertEqual(manifest["state"], "partial")
        self.assertLessEqual(len(manifest["errors"]), 64)
        self.assertEqual(cap.failures, 0)

    def test_invalid_manifest_is_never_published(self) -> None:
        cap = self.facade()
        before = (cap.session.directory / "manifest.json").read_text()
        cap.session._configuration_hash = "not-a-hash"
        cap.poll()
        self.assertEqual(cap.failures, 1)
        self.assertEqual((cap.session.directory / "manifest.json").read_text(), before)

    def test_destination_secrets_never_reach_disk(self) -> None:
        cap = self.facade()
        cap.destination_started(1)
        cap.destination_ended(1, "failed", 0, "destination_unavailable")
        self.segment(cap.session.directory, 1)
        cap.close()
        for path in cap.session.directory.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".txt"}:
                self.assertNotIn("SENTINEL", path.read_text())
                self.assertNotIn("rtmp", path.read_text())


class RetentionTests(Base):
    def make(self, session_id: str, *, days: int = 7, size: int = 100, hold: bool = False, finish: bool = True) -> Path:
        self.settings = CaptureSettings.from_mapping(raw(retentionDays=days, legalHold=hold), root=self.root)
        cap = self.facade(session_id=session_id, destination_count=1)
        cap.destination_started(1)
        self.clock.advance(seconds=1)
        cap.destination_ended(1, "accepted", 10, None)
        self.segment(cap.session.directory, 1, size)
        if finish:
            cap.close()
        return cap.session.directory

    def test_expired_sessions_are_deleted_and_the_rest_kept(self) -> None:
        old = self.make("11111111-1111-4111-8111-111111111111", days=1)
        fresh = self.make("22222222-2222-4222-8222-222222222222", days=30)
        held = self.make("33333333-3333-4333-8333-333333333333", days=1, hold=True)
        running = self.make("44444444-4444-4444-8444-444444444444", days=1, finish=False)
        (self.root / "not-a-session").mkdir()
        (self.root / "55555555-5555-4555-8555-555555555555").mkdir()
        self.clock.advance(days=2)
        result = apply_retention(self.root, self.clock(), 1_000_000)
        self.assertEqual(result["expired"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists() and held.exists() and running.exists())
        self.assertTrue((self.root / "not-a-session").exists())

    def test_quota_removes_oldest_terminal_sessions_first(self) -> None:
        first = self.make("11111111-1111-4111-8111-111111111111", size=400, days=30)
        self.clock.advance(minutes=1)
        second = self.make("22222222-2222-4222-8222-222222222222", size=400, days=30)
        self.assertGreater(usage(self.root), 1_000)
        result = apply_retention(self.root, self.clock(), 1_000)
        self.assertEqual(result["quota"], 1)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())

    def test_missing_root_and_under_quota_are_no_ops(self) -> None:
        self.assertEqual(apply_retention(self.root / "absent", self.clock(), 1), {"expired": 0, "quota": 0, "kept": 0})
        self.make("11111111-1111-4111-8111-111111111111")
        self.assertEqual(apply_retention(self.root, self.clock(), 10**9)["quota"], 0)

    def test_new_session_applies_retention_before_checking_quota(self) -> None:
        self.settings = CaptureSettings.from_mapping(raw(retentionDays=1, maxSessionBytes=50, maxTotalBytes=200), root=self.root)
        stale = self.make("11111111-1111-4111-8111-111111111111", days=1, size=150)
        self.clock.advance(days=3)
        fresh = self.facade(session_id="22222222-2222-4222-8222-222222222222")
        self.assertFalse(stale.exists())
        self.assertEqual(self.manifest(fresh)["state"], "recording")


if __name__ == "__main__":
    unittest.main()
