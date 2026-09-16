#!/usr/bin/env python3
from __future__ import annotations

import copy
import hashlib
import io
import json
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recording_manifest import (
    CONTRACT_VERSION,
    MAX_REPORT_BYTES,
    RecordingContractError,
    load_manifest,
    render_session_report,
    validate_manifest,
)


FIXTURES = Path(__file__).parent / "fixtures" / "recording"
SCHEMA = Path(__file__).resolve().parents[1] / "contracts" / "recording-session-v1.schema.json"


def synthetic_wav(duration_ms: int, storage_key: str) -> bytes:
    """Produce deterministic, license-free PCM used only by contract fixtures."""

    sample_rate = 8_000
    sample_count = duration_ms * sample_rate // 1_000
    seed = hashlib.sha256(storage_key.encode()).digest()[0]
    pcm = b"".join(
        struct.pack("<h", ((index * 257 + seed * 997) % 65_536) - 32_768)
        for index in range(sample_count)
    )
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + len(pcm),
        b"WAVE",
        b"fmt ",
        16,
        1,
        1,
        sample_rate,
        sample_rate * 2,
        2,
        16,
        b"data",
        len(pcm),
    )
    return header + pcm


def read_fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def materialize_fixture(manifest: dict[str, object], root: Path) -> None:
    capture = manifest["capture"]
    assert isinstance(capture, dict)
    artifacts = list(capture["segments"])
    if capture["finalArtifact"] is not None:
        artifacts.append(capture["finalArtifact"])
    for artifact in artifacts:
        path = root / artifact["storageKey"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(synthetic_wav(artifact["durationMs"], artifact["storageKey"]))


class RecordingManifestContractTest(unittest.TestCase):
    def test_synthetic_media_is_short_valid_license_free_pcm(self) -> None:
        payload = synthetic_wav(100, "artifacts/segment-000001.wav")
        with wave.open(io.BytesIO(payload), "rb") as media:
            self.assertEqual(media.getnchannels(), 1)
            self.assertEqual(media.getsampwidth(), 2)
            self.assertEqual(media.getframerate(), 8_000)
            self.assertEqual(media.getnframes(), 800)

    def test_synthetic_outcome_and_active_fixtures_verify_real_bytes(self) -> None:
        expected_states = {
            "active-in-flight": "recording",
            "complete": "complete",
            "partial": "partial",
            "crashed-recovered": "complete",
            "disk-full": "failed",
            "destination-failure": "partial",
            "preflight-failure": "failed",
        }
        for name, state in expected_states.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                manifest = read_fixture(name)
                root = Path(temporary)
                materialize_fixture(manifest, root)
                validated = validate_manifest(manifest, artifact_root=root)
                self.assertEqual(validated["state"], state)

                path = Path(temporary) / f"{name}.json"
                path.write_text(json.dumps(manifest), encoding="utf-8")
                self.assertEqual(load_manifest(path, artifact_root=root)["state"], state)

    def test_rejects_invalid_transitions_checksums_sequence_and_duration(self) -> None:
        mutations = {
            "invalid_transition": lambda value: value["transitions"][2].update(state="recording"),
            "missing_checksum": lambda value: value["capture"]["segments"][0].pop("sha256"),
            "bad_sequence": lambda value: value["capture"]["segments"][1].update(sequence=3),
            "bad_duration": lambda value: value["capture"].update(capturedDurationMs=201),
        }
        for reason, mutate in mutations.items():
            with self.subTest(reason=reason):
                manifest = read_fixture("complete")
                mutate(manifest)
                with self.assertRaises(RecordingContractError):
                    validate_manifest(manifest)

        timeline = read_fixture("crashed-recovered")
        timeline["capture"]["sourceTimeline"][1]["startedOffsetMs"] = 150
        with self.assertRaisesRegex(RecordingContractError, "source_timeline_mismatch"):
            validate_manifest(timeline)

    def test_active_and_pending_states_enforce_progress_invariants(self) -> None:
        active = read_fixture("active-in-flight")
        self.assertEqual(validate_manifest(active)["state"], "recording")
        active_report = render_session_report(active)
        self.assertIn("attempted:1 in_flight:1", active_report)
        self.assertIn("pending:1", active_report)

        active["capture"]["finalArtifact"] = copy.deepcopy(
            read_fixture("complete")["capture"]["finalArtifact"]
        )
        active["capture"]["finalArtifact"]["durationMs"] = 100
        with self.assertRaisesRegex(RecordingContractError, "active_capture_has_final_artifact"):
            validate_manifest(active)

        pending = read_fixture("preflight-failure")
        pending["state"] = "pending"
        pending["closedAt"] = None
        pending["transitions"] = pending["transitions"][:1]
        pending["destinations"][0]["finalStatus"] = "pending"
        pending["errors"] = []
        self.assertEqual(validate_manifest(pending)["state"], "pending")

        pending_delivery = copy.deepcopy(pending)
        pending_delivery["destinations"] = copy.deepcopy(read_fixture("complete")["destinations"])
        with self.assertRaisesRegex(RecordingContractError, "pending_delivery_has_progress"):
            validate_manifest(pending_delivery)

        pending_capture = copy.deepcopy(pending)
        pending_capture["capture"] = copy.deepcopy(read_fixture("active-in-flight")["capture"])
        with self.assertRaisesRegex(RecordingContractError, "pending_capture_has_progress"):
            validate_manifest(pending_capture)

    def test_terminal_retention_and_error_times_are_causal(self) -> None:
        expired = read_fixture("complete")
        expired["retention"]["deleteAfter"] = expired["closedAt"]
        with self.assertRaisesRegex(RecordingContractError, "invalid_retention"):
            validate_manifest(expired)

        noncausal = read_fixture("crashed-recovered")
        noncausal["errors"][0]["at"] = "2026-09-07T01:00:00.150Z"
        with self.assertRaisesRegex(RecordingContractError, "noncausal_error_time"):
            validate_manifest(noncausal)

        unordered = read_fixture("crashed-recovered")
        unordered["errors"].append(
            {"sequence": 2, "at": "2026-09-07T01:00:00.150Z", "phase": "capture", "code": "capture_interrupted", "retryable": False}
        )
        with self.assertRaisesRegex(RecordingContractError, "invalid_error_time"):
            validate_manifest(unordered)

    def test_terminal_failure_can_record_no_destination_attempt(self) -> None:
        manifest = read_fixture("preflight-failure")
        self.assertEqual(validate_manifest(manifest)["destinations"][0]["finalStatus"], "not_attempted")
        report = render_session_report(manifest)
        self.assertIn("attempted:0", report)
        self.assertIn("not_attempted:1", report)

    def test_retries_require_occurrence_aware_errors_and_final_timecodes_map(self) -> None:
        retries = read_fixture("destination-failure")
        self.assertEqual(validate_manifest(retries)["state"], "partial")
        self.assertIn("errors=destination_unavailable:2", render_session_report(retries))

        missing_retry_error = copy.deepcopy(retries)
        missing_retry_error["errors"].pop()
        with self.assertRaisesRegex(RecordingContractError, "noncausal_error_time"):
            validate_manifest(missing_retry_error)

        recovered = read_fixture("crashed-recovered")
        self.assertEqual(
            recovered["capture"]["finalArtifact"]["durationMs"],
            recovered["capture"]["elapsedDurationMs"],
        )
        recovered_report = render_session_report(recovered)
        self.assertIn("source_timeline=0:live,100:unobserved,300:live", recovered_report)
        self.assertIn("final=present duration_ms:400", recovered_report)

        collapsed_time = copy.deepcopy(recovered)
        collapsed_time["capture"]["finalArtifact"]["durationMs"] = 200
        with self.assertRaisesRegex(RecordingContractError, "inconsistent_duration"):
            validate_manifest(collapsed_time)

    def test_rejects_unsafe_or_identity_bearing_fields(self) -> None:
        for key, value in (
            ("streamUrl", "rtmps://example.invalid/private"),
            ("authorization", "Bearer fake-value"),
            ("userIdentifier", "person@example.invalid"),
            ("rawErrorMessage", "private failure text"),
        ):
            with self.subTest(key=key):
                manifest = read_fixture("disk-full")
                manifest[key] = value
                with self.assertRaisesRegex(RecordingContractError, "unsafe_field"):
                    validate_manifest(manifest)

        manifest = read_fixture("complete")
        manifest["sessionId"] = "person@example.invalid"
        with self.assertRaisesRegex(RecordingContractError, "invalid_session_id"):
            validate_manifest(manifest)

    def test_artifact_verification_detects_content_change(self) -> None:
        manifest = read_fixture("complete")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            materialize_fixture(manifest, root)
            target = root / "artifacts" / "segment-000001.wav"
            payload = bytearray(target.read_bytes())
            payload[-1] ^= 1
            target.write_bytes(payload)
            with self.assertRaisesRegex(RecordingContractError, "artifact_checksum_mismatch"):
                validate_manifest(manifest, artifact_root=root)

    def test_reports_are_exact_deterministic_bounded_and_redacted(self) -> None:
        complete = read_fixture("complete")
        expected = (
            "Croccante recording session report v1\n"
            "session=11111111-1111-4111-8111-111111111111 state=complete\n"
            "opened=2026-09-07T00:00:00Z closed=2026-09-07T00:00:00.500Z\n"
            "capture=segments:2 captured_ms:200 elapsed_ms:200 bytes:3288 dropped_frames:0 live_ms:200 filler_ms:0 unobserved_ms:0\n"
            "source_timeline=0:live\n"
            "final=present duration_ms:200 bytes:3244\n"
            "delivery=attempted:1 in_flight:0 accepted:1 failed:0 unknown:0 not_attempted:0 pending:0 observed_bytes:5000\n"
            "errors=none\n"
            "retention=public-broadcast delete_after=2026-10-07T00:00:00Z legal_hold=false\n"
        )
        self.assertEqual(render_session_report(complete), expected)
        self.assertEqual(render_session_report(complete), render_session_report(copy.deepcopy(complete)))

        maximum = copy.deepcopy(complete)
        maximum["destinations"] = []
        for slot in range(1, 21):
            destination = copy.deepcopy(complete["destinations"][0])
            destination["slot"] = slot
            maximum["destinations"].append(destination)
        maximum["errors"] = [
            {
                "sequence": sequence,
                "at": "2026-09-07T00:00:00.200Z",
                "phase": "recovery",
                "code": "process_crashed",
                "retryable": True,
            }
            for sequence in range(1, 65)
        ]
        maximum["capture"]["sourceTimeline"] = [
            {
                "sequence": sequence,
                "startedOffsetMs": sequence - 1,
                "mode": "live" if sequence % 2 else "filler",
            }
            for sequence in range(1, 65)
        ]
        self.assertLessEqual(len(render_session_report(maximum).encode("utf-8")), MAX_REPORT_BYTES)

        destination_failure = render_session_report(read_fixture("destination-failure"))
        self.assertIn("source_timeline=0:live,100:filler,180:live", destination_failure)

        forbidden = ("http://", "https://", "rtmp://", "rtmps://", "token", "secret", "password", "user_id", "raw error")
        for path in sorted(FIXTURES.glob("*.json")):
            with self.subTest(path=path.name):
                report = render_session_report(json.loads(path.read_text(encoding="utf-8")))
                self.assertLessEqual(len(report.encode("utf-8")), MAX_REPORT_BYTES)
                self.assertTrue(all(term not in report.lower() for term in forbidden))

    def test_json_loader_rejects_duplicate_fields_at_every_depth(self) -> None:
        payloads = (
            '{"contractVersion":"croccante.recording-session/v1","contractVersion":"croccante.recording-session/v1"}',
            '{"outer":{"state":"pending","state":"failed"}}',
        )
        for payload in payloads:
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "duplicate.json"
                path.write_text(payload, encoding="utf-8")
                with self.assertRaisesRegex(RecordingContractError, "duplicate_field"):
                    load_manifest(path)

    def test_json_schema_declares_same_version_and_closed_top_level(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema["properties"]["contractVersion"]["const"], CONTRACT_VERSION)
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(set(schema["required"]), set(schema["properties"]))


if __name__ == "__main__":
    unittest.main()
