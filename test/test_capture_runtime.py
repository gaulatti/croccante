#!/usr/bin/env python3
"""The always-on capture runtime: when a session opens, what it records, and that it never raises."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import capture_runtime
from as_aired_capture import AsAiredCapture
from capture_runtime import CaptureRuntime, stream_profile
from recording_manifest import validate_manifest

PROFILE = {"width": 160, "height": 90, "fps": 10, "audioRate": 44100, "audioChannels": 2}
HASH = "c" * 64
LIVE = "rtmp://127.0.0.1:1935/live/program"
FILLER = "/var/lib/croccante/fillers/abc/v1/filler.flv"
START = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


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

    def poll(self):
        return None if self.alive else 1

    def terminate(self) -> None:
        self.alive = False

    def wait(self, timeout: int) -> None:
        return None


def probe(path: Path) -> dict:
    video, audio = stream_profile(PROFILE)
    return {"durationMs": 6000, "video": video, "audio": audio}


class RuntimeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "as-aired"
        self.clock = Clock()
        self.commands: list[list[str]] = []
        self.processes: list[FakeProcess] = []
        self.runtime = CaptureRuntime(
            self.root, clock=self.clock, close_in_background=False, start=self.launch, probe=probe
        )

    def launch(self, command: list[str]) -> FakeProcess:
        self.commands.append(command)
        process = FakeProcess()
        self.processes.append(process)
        return process

    def begin(self, count: int = 2) -> None:
        self.runtime.start(profile=PROFILE, destination_count=count, configuration_hash=HASH)

    def segment(self, number: int) -> None:
        directory = next(self.root.iterdir()) / "artifacts"
        (directory / f"segment-{number:06d}.ts").write_bytes(bytes([number]) * 100)
        self.clock.advance(seconds=6)

    def manifests(self) -> list[dict]:
        return [json.loads((item / "manifest.json").read_text()) for item in sorted(self.root.iterdir())]


class ProfileTests(unittest.TestCase):
    def test_the_declared_profile_is_the_prepared_broadcast_profile(self) -> None:
        video, audio = stream_profile(PROFILE)
        self.assertEqual(video, {"codec": "h264", "width": 160, "height": 90, "frameRate": {"numerator": 10, "denominator": 1}})
        self.assertEqual(audio, {"codec": "aac", "sampleRateHz": 44100, "channels": 2})

    def test_the_storage_bounds_match_alanas_recording(self) -> None:
        self.assertEqual(capture_runtime.MAX_TOTAL_BYTES, 50 * 1024**3)
        self.assertEqual(capture_runtime.RETENTION_DAYS * 24, 168)
        self.assertLessEqual(capture_runtime.MAX_SESSION_BYTES, capture_runtime.MAX_TOTAL_BYTES)


class LifecycleTests(RuntimeCase):
    def test_nothing_is_recorded_until_media_is_on_air(self) -> None:
        self.begin()
        self.runtime.tick(live_source=None, filler_source=None, destinations=["waiting", "waiting"])
        self.assertFalse(self.root.exists())
        self.assertEqual(self.runtime.snapshot()["state"], "unavailable")

    def test_a_tick_before_start_records_nothing(self) -> None:
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.assertEqual(self.commands, [])

    def test_the_session_opens_with_the_first_live_media_and_records_the_relay_source(self) -> None:
        self.begin()
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying", "relaying"])
        self.assertEqual(len(self.commands), 1)
        self.assertEqual(self.commands[0][self.commands[0].index("-i") + 1], LIVE)
        self.assertEqual(self.runtime.snapshot()["state"], "recording")
        [manifest] = self.manifests()
        validate_manifest(manifest)
        self.assertEqual([slot["finalStatus"] for slot in manifest["destinations"]], ["pending", "pending"])

    def test_live_then_filler_then_live_are_transitions_and_the_process_follows_the_source(self) -> None:
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.segment(1)
        self.runtime.tick(live_source=None, filler_source=FILLER, destinations=["filler"])
        self.segment(2)
        self.segment(3)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        sources = [command[command.index("-i") + 1] for command in self.commands]
        self.assertEqual(sources, [LIVE, FILLER, LIVE])
        self.runtime.stop()
        [manifest] = self.manifests()
        validate_manifest(manifest)
        modes = [event["mode"] for event in manifest["capture"]["sourceTimeline"]]
        self.assertIn("filler", modes)
        self.assertEqual(modes[0], "live")
        self.assertEqual(manifest["state"] in {"complete", "partial"}, True)

    def test_stop_closes_to_a_terminal_manifest_with_a_report_and_accepted_attempts(self) -> None:
        self.begin(count=2)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying", "relaying"])
        self.segment(1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying", "relaying"])
        self.runtime.stop()
        [manifest] = self.manifests()
        validate_manifest(manifest)
        self.assertEqual(manifest["state"], "complete")
        self.assertEqual([slot["finalStatus"] for slot in manifest["destinations"]], ["accepted", "accepted"])
        self.assertTrue((next(self.root.iterdir()) / "report.txt").is_file())
        self.assertTrue(manifest["capture"]["finalArtifact"])
        self.assertEqual(self.runtime.snapshot()["state"], "complete")

    def test_a_destination_that_never_aired_bytes_closes_unknown(self) -> None:
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.runtime.stop()
        [manifest] = self.manifests()
        validate_manifest(manifest)
        self.assertEqual(manifest["destinations"][0]["finalStatus"], "unknown")
        self.assertEqual(manifest["state"], "partial")

    def test_a_destination_that_backs_off_records_a_failed_attempt_and_a_new_one_on_recovery(self) -> None:
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.segment(1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["backoff"])
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.segment(2)
        self.runtime.stop()
        [manifest] = self.manifests()
        validate_manifest(manifest)
        attempts = manifest["destinations"][0]["attempts"]
        self.assertEqual([attempt["status"] for attempt in attempts], ["failed", "accepted"])
        self.assertEqual(attempts[0]["errorCode"], "destination_unavailable")

    def test_a_forced_capture_process_death_is_interrupted_then_recovers_and_delivery_state_is_untouched(self) -> None:
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.processes[0].alive = False
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        [manifest] = self.manifests()
        self.assertIn("process_crashed", [item["reasonCode"] for item in manifest["transitions"]])
        self.assertEqual(len(self.processes), 2)
        self.assertEqual(self.runtime.snapshot()["recorder_failures"], 0)

    def test_the_next_start_opens_a_new_session_after_a_stop(self) -> None:
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.runtime.stop()
        self.clock.advance(seconds=1)
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.assertEqual(len(self.manifests()), 2)

    def test_stop_without_a_session_is_harmless(self) -> None:
        self.runtime.stop()
        self.begin()
        self.runtime.stop()
        self.assertEqual(self.runtime.snapshot()["state"], "unavailable")

    def test_stop_can_finalize_off_the_request_and_wait_for_it(self) -> None:
        background = CaptureRuntime(self.root, clock=self.clock, start=self.launch, probe=probe)
        background.start(profile=PROFILE, destination_count=1, configuration_hash=HASH)
        background.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        background.stop()
        background.wait(10)
        [manifest] = self.manifests()
        self.assertIn(manifest["state"], {"partial", "complete"})


class FollowTests(RuntimeCase):
    def sources(self) -> list[str]:
        return [command[command.index("-i") + 1] for command in self.commands]

    def test_a_new_publisher_under_the_same_mode_is_followed(self) -> None:
        other = "rtmp://127.0.0.1:1935/live/encoder-b"
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.assertEqual(self.sources(), [LIVE])
        self.runtime.tick(live_source=other, filler_source=None, destinations=["relaying"])
        self.assertEqual(self.sources(), [LIVE, other])
        self.assertTrue(self.processes[0].alive is False)
        self.assertEqual(self.runtime.snapshot()["state"], "recording")

    def test_a_new_filler_version_under_the_same_mode_is_followed(self) -> None:
        newer = "/var/lib/croccante/fillers/abc/v2/filler.flv"
        self.begin(count=1)
        self.runtime.tick(live_source=None, filler_source=FILLER, destinations=["filler"])
        self.runtime.tick(live_source=None, filler_source=newer, destinations=["filler"])
        self.assertEqual(self.sources(), [FILLER, newer])

    def test_a_slot_that_leaves_the_air_without_failing_ends_its_attempt_there(self) -> None:
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.segment(1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.segment(2)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["idle"])
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.runtime.stop()
        [manifest] = self.manifests()
        validate_manifest(manifest)
        attempts = manifest["destinations"][0]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["status"], "accepted")
        self.assertIsNotNone(attempts[0]["endedAt"])

    def test_a_capture_process_that_keeps_dying_is_restarted_with_a_growing_pause(self) -> None:
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        launches = [0]
        for second in range(1, 40):
            self.clock.advance(seconds=1)
            before = len(self.processes)
            self.processes[-1].alive = False
            self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
            if len(self.processes) > before:
                launches.append(second)
        gaps = [later - earlier for earlier, later in zip(launches, launches[1:])]
        # The first restart follows the crash at once; each restart that records nothing doubles the
        # pause before the next (1, 1, 2, 4, 8, 16 seconds between launches here).
        self.assertEqual(gaps, [1, 1, 2, 4, 8, 16])

    def test_a_restart_that_records_resets_the_pause(self) -> None:
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.processes[-1].alive = False
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.segment(1)
        self.segment(2)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        self.assertEqual(self.runtime._recoveries, 0)

    def test_the_snapshot_never_waits_for_the_recorder(self) -> None:
        self.begin(count=1)
        self.runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        result: list[dict] = []
        with self.runtime._lock:  # a tick is in the middle of slow I/O
            reader = __import__("threading").Thread(target=lambda: result.append(self.runtime.snapshot()))
            reader.start()
            reader.join(2)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["state"], "recording")


class IsolationTests(RuntimeCase):
    def test_a_raising_recorder_never_raises_into_the_relay_and_is_counted(self) -> None:
        class Raising:
            failures = 0

            def __init__(self, *args, **kwargs) -> None:
                raise OSError("disk unavailable")

        runtime = CaptureRuntime(self.root, capture_class=Raising)
        runtime.start(profile=PROFILE, destination_count=1, configuration_hash=HASH)
        runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        runtime.stop()
        self.assertEqual(runtime.snapshot()["recorder_failures"], 1)

    def test_a_capture_that_raises_later_is_counted_and_stop_still_returns(self) -> None:
        class Flaky(AsAiredCapture):
            def poll(self) -> None:
                raise RuntimeError("probe failed")

        runtime = CaptureRuntime(self.root, capture_class=Flaky, clock=self.clock, close_in_background=False, start=self.launch, probe=probe)
        runtime.start(profile=PROFILE, destination_count=1, configuration_hash=HASH)
        runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        runtime.stop()
        self.assertGreaterEqual(runtime.snapshot()["recorder_failures"], 1)

    def test_a_close_that_raises_is_counted(self) -> None:
        class Broken(AsAiredCapture):
            def close(self) -> None:
                raise RuntimeError("cannot finalize")

        runtime = CaptureRuntime(self.root, capture_class=Broken, clock=self.clock, close_in_background=False, start=self.launch, probe=probe)
        runtime.start(profile=PROFILE, destination_count=1, configuration_hash=HASH)
        runtime.tick(live_source=LIVE, filler_source=None, destinations=["relaying"])
        runtime.stop()
        self.assertEqual(runtime.snapshot()["recorder_failures"], 1)

    def test_a_stop_that_cannot_read_the_capture_is_counted_not_raised(self) -> None:
        class Stub:
            failures = 0

            def __init__(self, *args, **kwargs) -> None:
                self.reads = 0

            def note_source(self, mode: str) -> None:
                return None

            def poll(self) -> None:
                return None

            def recover(self) -> None:
                return None

            def snapshot(self) -> dict:
                self.reads += 1
                if self.reads > 2:
                    raise OSError("capture state unreadable")
                return {"state": "recording", "segments": 0, "bytes": 0}

        runtime = CaptureRuntime(self.root, capture_class=Stub, clock=self.clock)
        runtime.start(profile=PROFILE, destination_count=1, configuration_hash=HASH)
        runtime.tick(live_source=LIVE, filler_source=None, destinations=[])
        runtime.stop()
        self.assertEqual(runtime._failures, 1)

    def test_guard_counts_a_fault_in_whatever_feeds_the_recorder(self) -> None:
        def boom() -> None:
            raise ValueError("unreadable state")

        self.runtime.guard(boom)
        self.runtime.guard(lambda: None)
        self.assertEqual(self.runtime.snapshot()["recorder_failures"], 1)

    def test_a_bad_profile_at_start_is_counted_not_raised(self) -> None:
        self.runtime.start(profile={"width": 1}, destination_count=1, configuration_hash=HASH)
        self.assertEqual(self.runtime.snapshot()["recorder_failures"], 1)


if __name__ == "__main__":
    unittest.main()
