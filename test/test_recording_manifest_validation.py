#!/usr/bin/env python3
"""Rejection matrix for the recording manifest contract.

Every case mutates a real fixture and asserts the exact stable reason code, so
each validation branch is proven by the input that must trigger it.
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import recording_manifest
from recording_manifest import (
    RecordingContractError,
    load_manifest,
    render_session_report,
    validate_manifest,
)
from test_recording_manifest import materialize_fixture, read_fixture

FAILURE_ERROR = {
    "sequence": 1,
    "at": "2026-09-07T00:00:00.100Z",
    "phase": "storage",
    "code": "disk_full",
    "retryable": False,
}


def attempt(**changes: object) -> dict[str, object]:
    value: dict[str, object] = {
        "sequence": 1,
        "startedAt": "2026-09-07T00:00:00.100Z",
        "endedAt": "2026-09-07T00:00:00.450Z",
        "status": "accepted",
        "observedBytes": 5000,
        "errorCode": None,
    }
    value.update(changes)
    return value


def pending_manifest() -> dict[str, object]:
    pending = read_fixture("preflight-failure")
    pending["state"] = "pending"
    pending["closedAt"] = None
    pending["transitions"] = pending["transitions"][:1]
    pending["destinations"][0]["finalStatus"] = "pending"
    pending["errors"] = []
    return pending


def with_video(manifest: dict[str, object], **changes: object) -> None:
    video: dict[str, object] = {
        "codec": "h264",
        "width": 160,
        "height": 90,
        "frameRate": {"numerator": 10, "denominator": 1},
    }
    video.update(changes)
    manifest["capture"]["profile"]["video"] = video  # type: ignore[index]


def long_wall_clock(manifest: dict[str, object]) -> None:
    capture = manifest["capture"]
    capture["elapsedDurationMs"] = 600  # type: ignore[index]
    capture["finalArtifact"]["durationMs"] = 600  # type: ignore[index]
    capture["sourceTimeline"].append(  # type: ignore[index]
        {"sequence": 2, "startedOffsetMs": 200, "mode": "unobserved"}
    )


def failed_with_final(manifest: dict[str, object]) -> None:
    final = copy.deepcopy(read_fixture("complete")["capture"]["finalArtifact"])
    final["durationMs"] = manifest["capture"]["elapsedDurationMs"]  # type: ignore[index]
    manifest["capture"]["finalArtifact"] = final  # type: ignore[index]


def failed_with_pending_delivery(manifest: dict[str, object]) -> None:
    manifest["destinations"] = [
        {
            "slot": 1,
            "finalStatus": "pending",
            "attempts": [
                attempt(
                    startedAt="2026-09-07T03:00:00.100Z",
                    status="in_progress",
                    endedAt=None,
                    observedBytes=0,
                )
            ],
        }
    ]


CASES: list[tuple[str, str, object, str]] = [
    # (name, fixture, mutation, reason)
    ("manifest-not-object", "complete", lambda m: None, "invalid_manifest"),
    ("version", "complete", lambda m: m.update(contractVersion="v2"), "unsupported_contract_version"),
    ("session-type", "complete", lambda m: m.update(sessionId=5), "invalid_session_id"),
    ("configuration-type", "complete", lambda m: m.update(configurationHash=5), "invalid_configuration_hash"),
    ("configuration-shape", "complete", lambda m: m.update(configurationHash="abc"), "invalid_configuration_hash"),
    ("state-unknown", "complete", lambda m: m.update(state="bogus"), "invalid_state"),
    ("state-type", "complete", lambda m: m.update(state=5), "invalid_state"),
    ("opened-at-unparseable", "complete", lambda m: m.update(openedAt="nope"), "invalid_opened_at"),
    ("opened-at-impossible-date", "complete", lambda m: m.update(openedAt="2026-13-45T00:00:00Z"), "invalid_opened_at"),
    ("opened-at-mismatch", "complete", lambda m: m.update(openedAt="2026-09-07T00:00:01Z"), "opened_at_mismatch"),
    ("closed-at-missing", "complete", lambda m: m.update(closedAt=None), "invalid_closed_at"),
    ("closed-at-mismatch", "complete", lambda m: m.update(closedAt="2026-09-07T00:00:01Z"), "closed_at_mismatch"),
    ("closed-at-on-active", "active-in-flight", lambda m: m.update(closedAt="2026-09-07T00:00:01Z"), "unexpected_closed_at"),
    ("elapsed-exceeds-wall-clock", "complete", long_wall_clock, "inconsistent_duration"),
    ("unsafe-nested-key", "complete", lambda m: m["capture"]["segments"][0].update(secretValue=1), "unsafe_field"),
    # transitions
    ("transitions-type", "complete", lambda m: m.update(transitions="x"), "invalid_transitions"),
    ("transitions-empty", "complete", lambda m: m.update(transitions=[]), "invalid_transitions"),
    ("transitions-unbounded", "complete", lambda m: m.update(transitions=[m["transitions"][0]] * 65), "invalid_transitions"),
    ("transition-type", "complete", lambda m: m["transitions"].__setitem__(1, "x"), "invalid_transition"),
    ("transition-time", "complete", lambda m: m["transitions"][1].update(at="bad"), "invalid_transition_time"),
    ("transition-sequence-type", "complete", lambda m: m["transitions"][1].update(sequence="2"), "invalid_transition"),
    ("transition-state-type", "complete", lambda m: m["transitions"][1].update(state=5), "invalid_transition"),
    ("transition-state-unknown", "complete", lambda m: m["transitions"][1].update(state="bogus"), "invalid_transition"),
    ("transition-first-not-pending", "complete", lambda m: m["transitions"][0].update(state="recording"), "invalid_transition"),
    ("transition-backwards-time", "complete", lambda m: m["transitions"][2].update(at="2026-09-07T00:00:00.050Z"), "invalid_transition_time"),
    ("transition-reason-unknown", "complete", lambda m: m["transitions"][1].update(reasonCode="bogus"), "invalid_error_code"),
    ("transition-reason-type", "complete", lambda m: m["transitions"][1].update(reasonCode=5), "invalid_error_code"),
    ("transition-unexpected-reason", "complete", lambda m: m["transitions"][1].update(reasonCode="disk_full"), "unexpected_transition_reason"),
    ("transition-missing-reason", "crashed-recovered", lambda m: m["transitions"][2].update(reasonCode=None), "missing_transition_reason"),
    ("transition-state-mismatch", "complete", lambda m: m.update(state="partial"), "state_mismatch"),
    (
        "transition-after-terminal",
        "complete",
        lambda m: m["transitions"].append({"sequence": 5, "state": "recording", "at": "2026-09-07T00:00:00.600Z", "reasonCode": None}),
        "invalid_transition",
    ),
    # artifacts
    ("segment-type", "complete", lambda m: m["capture"]["segments"].__setitem__(0, "x"), "invalid_artifact"),
    ("segment-kind", "complete", lambda m: m["capture"]["segments"][0].update(kind="final"), "invalid_artifact"),
    ("storage-key-type", "complete", lambda m: m["capture"]["segments"][0].update(storageKey=5), "unsafe_storage_key"),
    ("storage-key-traversal", "complete", lambda m: m["capture"]["segments"][0].update(storageKey="../x"), "unsafe_storage_key"),
    ("segment-duration-zero", "complete", lambda m: m["capture"]["segments"][0].update(durationMs=0), "invalid_duration"),
    ("segment-duration-type", "complete", lambda m: m["capture"]["segments"][0].update(durationMs=True), "invalid_duration"),
    ("segment-duration-max", "complete", lambda m: m["capture"]["segments"][0].update(durationMs=86_400_001), "invalid_duration"),
    ("segment-bytes-zero", "complete", lambda m: m["capture"]["segments"][0].update(byteLength=0), "invalid_byte_length"),
    ("segment-bytes-max", "complete", lambda m: m["capture"]["segments"][0].update(byteLength=1_099_511_627_777), "invalid_byte_length"),
    ("segment-checksum-type", "complete", lambda m: m["capture"]["segments"][0].update(sha256=5), "missing_or_invalid_checksum"),
    ("segment-checksum-shape", "complete", lambda m: m["capture"]["segments"][0].update(sha256="abc"), "missing_or_invalid_checksum"),
    ("segment-sequence-type", "complete", lambda m: m["capture"]["segments"][0].update(sequence="1"), "invalid_segment_sequence"),
    ("segment-sequence-value", "complete", lambda m: m["capture"]["segments"][0].update(sequence=2), "invalid_segment_sequence"),
    ("segment-key-sequence", "complete", lambda m: m["capture"]["segments"][0].update(storageKey="artifacts/segment-000009.wav"), "invalid_segment_sequence"),
    ("segment-offset-negative", "complete", lambda m: m["capture"]["segments"][0].update(startedOffsetMs=-1), "invalid_segment_timing"),
    ("segment-offset-type", "complete", lambda m: m["capture"]["segments"][0].update(startedOffsetMs="0"), "invalid_segment_timing"),
    ("final-key-not-final", "complete", lambda m: m["capture"]["finalArtifact"].update(storageKey="artifacts/segment-000009.wav"), "invalid_artifact"),
    # capture
    ("capture-type", "complete", lambda m: m.update(capture="x"), "invalid_capture"),
    ("profile-type", "complete", lambda m: m["capture"].update(profile="x"), "invalid_media_profile"),
    ("container-type", "complete", lambda m: m["capture"]["profile"].update(container=5), "invalid_media_profile"),
    ("container-shape", "complete", lambda m: m["capture"]["profile"].update(container="BAD"), "invalid_media_profile"),
    ("video-type", "complete", lambda m: m["capture"]["profile"].update(video=5), "invalid_media_profile"),
    ("video-frame-rate-type", "complete", lambda m: with_video(m, frameRate=5), "invalid_media_profile"),
    ("video-codec", "complete", lambda m: with_video(m, codec=5), "invalid_media_profile"),
    ("video-width", "complete", lambda m: with_video(m, width=0), "invalid_media_profile"),
    ("video-height", "complete", lambda m: with_video(m, height=True), "invalid_media_profile"),
    ("video-numerator", "complete", lambda m: with_video(m, frameRate={"numerator": 0, "denominator": 1}), "invalid_media_profile"),
    ("video-denominator", "complete", lambda m: with_video(m, frameRate={"numerator": 1, "denominator": 1002}), "invalid_media_profile"),
    ("audio-type", "complete", lambda m: m["capture"]["profile"].update(audio=5), "invalid_media_profile"),
    ("audio-codec", "complete", lambda m: m["capture"]["profile"]["audio"].update(codec=5), "invalid_media_profile"),
    ("audio-rate", "complete", lambda m: m["capture"]["profile"]["audio"].update(sampleRateHz=1), "invalid_media_profile"),
    ("audio-channels", "complete", lambda m: m["capture"]["profile"]["audio"].update(channels=0), "invalid_media_profile"),
    ("profile-without-media", "complete", lambda m: m["capture"]["profile"].update(audio=None), "invalid_media_profile"),
    ("time-base", "complete", lambda m: m["capture"]["profile"].update(timeBase="seconds"), "invalid_media_profile"),
    ("elapsed-negative", "complete", lambda m: m["capture"].update(elapsedDurationMs=-1), "invalid_duration"),
    ("captured-type", "complete", lambda m: m["capture"].update(capturedDurationMs="x"), "invalid_duration"),
    ("dropped-frames", "complete", lambda m: m["capture"].update(droppedFrames=-1), "invalid_dropped_frames"),
    ("timeline-type", "complete", lambda m: m["capture"].update(sourceTimeline="x"), "invalid_source_timeline"),
    ("timeline-unbounded", "complete", lambda m: m["capture"].update(sourceTimeline=[{}] * 65), "invalid_source_timeline"),
    ("timeline-event-type", "complete", lambda m: m["capture"]["sourceTimeline"].__setitem__(0, "x"), "invalid_source_event"),
    ("timeline-event-mode", "complete", lambda m: m["capture"]["sourceTimeline"][0].update(mode="bogus"), "invalid_source_event"),
    ("timeline-event-mode-type", "complete", lambda m: m["capture"]["sourceTimeline"][0].update(mode=5), "invalid_source_event"),
    ("timeline-event-sequence", "complete", lambda m: m["capture"]["sourceTimeline"][0].update(sequence=2), "invalid_source_event"),
    ("timeline-event-offset", "complete", lambda m: m["capture"]["sourceTimeline"][0].update(startedOffsetMs=-1), "invalid_source_event"),
    (
        "timeline-event-order",
        "complete",
        lambda m: m["capture"]["sourceTimeline"].append({"sequence": 2, "startedOffsetMs": 0, "mode": "filler"}),
        "invalid_source_event",
    ),
    (
        "timeline-event-repeats-mode",
        "complete",
        lambda m: m["capture"]["sourceTimeline"].append({"sequence": 2, "startedOffsetMs": 50, "mode": "live"}),
        "invalid_source_event",
    ),
    ("segments-type", "complete", lambda m: m["capture"].update(segments="x"), "invalid_segments"),
    ("segments-unbounded", "complete", lambda m: m["capture"].update(segments=[None] * 10_001), "invalid_segments"),
    ("segments-overlap", "complete", lambda m: m["capture"]["segments"][1].update(startedOffsetMs=50), "invalid_segment_timing"),
    ("captured-total", "complete", lambda m: m["capture"].update(capturedDurationMs=199), "inconsistent_duration"),
    ("elapsed-before-last-segment", "complete", lambda m: m["capture"].update(elapsedDurationMs=150), "inconsistent_duration"),
    ("empty-capture-with-timeline", "preflight-failure", lambda m: m["capture"]["sourceTimeline"].append({"sequence": 1, "startedOffsetMs": 0, "mode": "live"}), "invalid_source_timeline"),
    ("elapsed-without-timeline", "complete", lambda m: m["capture"].update(sourceTimeline=[]), "invalid_source_timeline"),
    ("timeline-not-from-zero", "complete", lambda m: m["capture"]["sourceTimeline"][0].update(startedOffsetMs=5), "invalid_source_timeline"),
    (
        "timeline-event-after-end",
        "complete",
        lambda m: m["capture"]["sourceTimeline"].append({"sequence": 2, "startedOffsetMs": 200, "mode": "filler"}),
        "invalid_source_timeline",
    ),
    ("final-duration", "complete", lambda m: m["capture"]["finalArtifact"].update(durationMs=100), "inconsistent_duration"),
    ("complete-without-final", "complete", lambda m: m["capture"].update(finalArtifact=None), "incomplete_capture"),
    ("failed-with-final", "disk-full", failed_with_final, "failed_capture_has_final_artifact"),
    # destinations
    ("destinations-type", "complete", lambda m: m.update(destinations="x"), "invalid_destinations"),
    ("destinations-empty", "complete", lambda m: m.update(destinations=[]), "invalid_destinations"),
    ("destinations-unbounded", "complete", lambda m: m.update(destinations=[m["destinations"][0]] * 21), "invalid_destinations"),
    ("pending-destination-type", "pending", lambda m: m.update(destinations=["x"]), "pending_delivery_has_progress"),
    ("destination-type", "complete", lambda m: m["destinations"].__setitem__(0, "x"), "invalid_destination"),
    ("destination-slot", "complete", lambda m: m["destinations"][0].update(slot=2), "invalid_destination"),
    ("destination-status-type", "complete", lambda m: m["destinations"][0].update(finalStatus=5), "invalid_destination"),
    ("destination-status-unknown", "complete", lambda m: m["destinations"][0].update(finalStatus="bogus"), "invalid_destination"),
    ("attempts-type", "complete", lambda m: m["destinations"][0].update(attempts="x"), "invalid_destination_attempts"),
    ("attempts-unbounded", "complete", lambda m: m["destinations"][0].update(attempts=[None] * 33), "invalid_destination_attempts"),
    ("attempt-type", "complete", lambda m: m["destinations"][0]["attempts"].__setitem__(0, "x"), "invalid_destination_attempt"),
    ("attempt-start-time", "complete", lambda m: m["destinations"][0]["attempts"][0].update(startedAt="bad"), "invalid_destination_attempt_time"),
    ("attempt-end-time", "complete", lambda m: m["destinations"][0]["attempts"][0].update(endedAt="bad"), "invalid_destination_attempt_time"),
    ("attempt-sequence", "complete", lambda m: m["destinations"][0]["attempts"][0].update(sequence=2), "invalid_destination_attempt"),
    ("attempt-status-type", "complete", lambda m: m["destinations"][0]["attempts"][0].update(status=5), "invalid_destination_attempt"),
    ("attempt-status-unknown", "complete", lambda m: m["destinations"][0]["attempts"][0].update(status="bogus"), "invalid_destination_attempt"),
    ("attempt-before-open", "complete", lambda m: m["destinations"][0]["attempts"][0].update(startedAt="2026-08-01T00:00:00Z"), "invalid_destination_attempt"),
    (
        "attempt-overlaps-previous",
        "complete",
        lambda m: m["destinations"][0]["attempts"].insert(0, attempt(startedAt="2026-09-07T00:00:00.200Z", endedAt="2026-09-07T00:00:00.300Z", status="unknown", errorCode="capture_interrupted"))
        or m["destinations"][0]["attempts"][1].update(sequence=2, startedAt="2026-09-07T00:00:00.250Z"),
        "invalid_destination_attempt",
    ),
    ("attempt-after-close", "complete", lambda m: m["destinations"][0]["attempts"][0].update(startedAt="2026-09-07T00:00:00.600Z", endedAt="2026-09-07T00:00:00.700Z"), "invalid_destination_attempt"),
    ("attempt-bytes-negative", "complete", lambda m: m["destinations"][0]["attempts"][0].update(observedBytes=-1), "invalid_destination_attempt"),
    ("attempt-bytes-type", "complete", lambda m: m["destinations"][0]["attempts"][0].update(observedBytes=True), "invalid_destination_attempt"),
    ("in-progress-with-end", "active-in-flight", lambda m: m["destinations"][0]["attempts"][0].update(endedAt="2026-09-07T00:00:00.200Z"), "invalid_in_progress_attempt"),
    ("in-progress-with-error", "active-in-flight", lambda m: m["destinations"][0]["attempts"][0].update(errorCode="disk_full"), "invalid_in_progress_attempt"),
    (
        "in-progress-not-last",
        "active-in-flight",
        lambda m: m["destinations"][0]["attempts"].append(attempt(sequence=2, startedAt="2026-09-07T00:00:00.200Z", endedAt="2026-09-07T00:00:00.300Z")),
        "invalid_in_progress_attempt",
    ),
    ("finished-without-end", "complete", lambda m: m["destinations"][0]["attempts"][0].update(endedAt=None), "invalid_destination_attempt"),
    ("finished-before-start", "complete", lambda m: m["destinations"][0]["attempts"][0].update(endedAt="2026-09-07T00:00:00.050Z"), "invalid_destination_attempt"),
    ("finished-after-close", "complete", lambda m: m["destinations"][0]["attempts"][0].update(endedAt="2026-09-07T00:00:00.600Z"), "invalid_destination_attempt"),
    ("accepted-with-error", "complete", lambda m: m["destinations"][0]["attempts"][0].update(errorCode="disk_full"), "unexpected_destination_error"),
    ("accepted-without-bytes", "complete", lambda m: m["destinations"][0]["attempts"][0].update(observedBytes=0), "missing_destination_bytes"),
    (
        "failed-without-error",
        "complete",
        lambda m: m["destinations"][0]["attempts"][0].update(status="failed") or m["destinations"][0].update(finalStatus="failed"),
        "missing_or_invalid_destination_error",
    ),
    (
        "failed-unknown-error",
        "complete",
        lambda m: m["destinations"][0]["attempts"][0].update(status="failed", errorCode="bogus") or m["destinations"][0].update(finalStatus="failed"),
        "missing_or_invalid_destination_error",
    ),
    ("final-status-mismatch", "complete", lambda m: m["destinations"][0].update(finalStatus="failed"), "destination_status_mismatch"),
    (
        "complete-with-failed-delivery",
        "complete",
        lambda m: m["destinations"][0]["attempts"][0].update(status="failed", errorCode="destination_unavailable") or m["destinations"][0].update(finalStatus="failed"),
        "incomplete_delivery",
    ),
    ("terminal-with-pending-delivery", "disk-full", failed_with_pending_delivery, "incomplete_delivery"),
    # errors
    ("errors-type", "complete", lambda m: m.update(errors="x"), "invalid_errors"),
    ("errors-unbounded", "complete", lambda m: m.update(errors=[FAILURE_ERROR] * 65), "invalid_errors"),
    ("error-type", "disk-full", lambda m: m["errors"].__setitem__(0, "x"), "invalid_error"),
    ("error-sequence", "disk-full", lambda m: m["errors"][0].update(sequence=2), "invalid_error"),
    ("error-phase-type", "disk-full", lambda m: m["errors"][0].update(phase=5), "invalid_error"),
    ("error-phase", "disk-full", lambda m: m["errors"][0].update(phase="bogus"), "invalid_error"),
    ("error-code-type", "disk-full", lambda m: m["errors"][0].update(code=5), "invalid_error"),
    ("error-code", "disk-full", lambda m: m["errors"][0].update(code="bogus"), "invalid_error"),
    ("error-retryable", "disk-full", lambda m: m["errors"][0].update(retryable="no"), "invalid_error"),
    ("error-time", "disk-full", lambda m: m["errors"][0].update(at="bad"), "invalid_error_time"),
    ("error-before-open", "disk-full", lambda m: m["errors"][0].update(at="2026-08-01T00:00:00Z"), "invalid_error_time"),
    ("error-after-close", "disk-full", lambda m: m["errors"][0].update(at="2027-01-01T00:00:00Z"), "invalid_error_time"),
    ("terminal-without-error", "disk-full", lambda m: m.update(errors=[]), "missing_terminal_error"),
    ("pending-with-error", "pending", lambda m: m.update(errors=[{**FAILURE_ERROR, "at": m["openedAt"]}]), "pending_session_has_errors"),
    ("transition-reason-without-error", "crashed-recovered", lambda m: m.update(errors=[]), "missing_structured_error"),
    # retention
    ("retention-type", "complete", lambda m: m.update(retention="x"), "invalid_retention"),
    ("retention-classification-type", "complete", lambda m: m["retention"].update(classification=5), "invalid_retention"),
    ("retention-classification", "complete", lambda m: m["retention"].update(classification="bogus"), "invalid_retention"),
    ("retention-hold-type", "complete", lambda m: m["retention"].update(legalHold="yes"), "invalid_retention"),
    ("retention-hold-with-deadline", "complete", lambda m: m["retention"].update(legalHold=True), "invalid_retention"),
    ("retention-without-deadline", "complete", lambda m: m["retention"].update(deleteAfter=None), "invalid_retention"),
    ("retention-deadline-unparseable", "complete", lambda m: m["retention"].update(deleteAfter="soon"), "invalid_retention"),
]


class RecordingManifestRejectionTest(unittest.TestCase):
    def test_every_rejection_names_its_stable_reason(self) -> None:
        for name, fixture, mutate, reason in CASES:
            with self.subTest(case=name):
                if fixture == "pending":
                    manifest = pending_manifest()
                else:
                    manifest = read_fixture(fixture)
                if name == "manifest-not-object":
                    manifest = ["not", "an", "object"]  # type: ignore[assignment]
                else:
                    mutate(manifest)  # type: ignore[operator]
                with self.assertRaises(RecordingContractError) as raised:
                    validate_manifest(manifest)
                self.assertEqual(raised.exception.reason, reason)

    def test_unsafe_keys_are_rejected_inside_lists(self) -> None:
        with self.assertRaisesRegex(RecordingContractError, "unsafe_field"):
            validate_manifest([{"token": "fake"}])

    def test_legal_hold_retention_and_video_profiles_are_accepted(self) -> None:
        manifest = read_fixture("complete")
        manifest["retention"] = {"classification": "restricted", "deleteAfter": None, "legalHold": True}
        with_video(manifest)
        self.assertEqual(validate_manifest(manifest)["state"], "complete")
        self.assertIn("legal_hold=true", render_session_report(manifest))
        self.assertIn("delete_after=legal-hold", render_session_report(manifest))

    def test_attempts_may_follow_a_prior_attempt_and_retry(self) -> None:
        manifest = read_fixture("destination-failure")
        self.assertEqual(validate_manifest(manifest)["state"], "partial")
        retry = read_fixture("complete")
        retry["destinations"][0]["attempts"].insert(
            0,
            attempt(
                startedAt="2026-09-07T00:00:00.100Z",
                endedAt="2026-09-07T00:00:00.200Z",
                status="unknown",
                errorCode="capture_interrupted",
            ),
        )
        retry["destinations"][0]["attempts"][1].update(sequence=2, startedAt="2026-09-07T00:00:00.250Z")
        retry["errors"] = [
            {
                "sequence": 1,
                "at": "2026-09-07T00:00:00.200Z",
                "phase": "delivery",
                "code": "capture_interrupted",
                "retryable": True,
            }
        ]
        self.assertEqual(validate_manifest(retry)["state"], "complete")

    def test_loader_rejects_unreadable_and_malformed_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(RecordingContractError, "invalid_manifest_json"):
                load_manifest(root / "missing.json")
            broken = root / "broken.json"
            broken.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(RecordingContractError, "invalid_manifest_json"):
                load_manifest(broken)

    def test_report_refuses_to_exceed_its_byte_bound(self) -> None:
        manifest = read_fixture("complete")
        with patch.object(recording_manifest, "MAX_REPORT_BYTES", 16):
            with self.assertRaisesRegex(RecordingContractError, "report_too_large"):
                render_session_report(manifest)


class ArtifactVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = read_fixture("complete")
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        self.root.mkdir()
        materialize_fixture(self.manifest, self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_intact_artifacts_verify(self) -> None:
        self.assertEqual(validate_manifest(self.manifest, artifact_root=self.root)["state"], "complete")

    def test_missing_artifact_is_unavailable(self) -> None:
        (self.root / "artifacts" / "final.wav").unlink()
        with self.assertRaisesRegex(RecordingContractError, "artifact_unavailable"):
            validate_manifest(self.manifest, artifact_root=self.root)

    def test_wrong_size_is_rejected_before_hashing(self) -> None:
        target = self.root / "artifacts" / "segment-000001.wav"
        target.write_bytes(target.read_bytes() + b"x")
        with self.assertRaisesRegex(RecordingContractError, "artifact_size_mismatch"):
            validate_manifest(self.manifest, artifact_root=self.root)

    def test_unreadable_artifact_is_unavailable(self) -> None:
        real_open = Path.open

        def refuse_binary(path: Path, mode: str = "r", *args: object, **kwargs: object):
            if mode == "rb":
                raise OSError("injected read failure")
            return real_open(path, mode, *args, **kwargs)  # type: ignore[arg-type]

        with patch.object(Path, "open", refuse_binary):
            with self.assertRaisesRegex(RecordingContractError, "artifact_unavailable"):
                validate_manifest(self.manifest, artifact_root=self.root)

    def test_storage_directory_cannot_escape_the_artifact_root(self) -> None:
        outside = Path(self.temporary.name) / "outside"
        (self.root / "artifacts").rename(outside)
        (self.root / "artifacts").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(RecordingContractError, "unsafe_storage_key"):
            validate_manifest(self.manifest, artifact_root=self.root)

    def test_loader_verifies_artifacts_it_names(self) -> None:
        path = Path(self.temporary.name) / "manifest.json"
        path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.assertEqual(load_manifest(path, artifact_root=self.root)["state"], "complete")


if __name__ == "__main__":
    unittest.main()
