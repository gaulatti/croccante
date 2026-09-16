#!/usr/bin/env python3
"""Versioned, credential-free recording manifest and session report contract."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


CONTRACT_VERSION = "croccante.recording-session/v1"
MAX_SEGMENTS = 10_000
MAX_SOURCE_EVENTS = 64
MAX_TRANSITIONS = 64
MAX_DESTINATIONS = 20
MAX_ATTEMPTS = 32
MAX_ERRORS = 64
MAX_REPORT_BYTES = 4_096
MAX_COUNTER = 9_223_372_036_854_775_807

STATES = frozenset(
    {"pending", "recording", "interrupted", "recovering", "finalizing", "complete", "partial", "failed"}
)
TERMINAL_STATES = frozenset({"complete", "partial", "failed"})
ALLOWED_TRANSITIONS = {
    "pending": frozenset({"recording", "failed"}),
    "recording": frozenset({"interrupted", "finalizing", "failed"}),
    "interrupted": frozenset({"recovering", "finalizing", "failed"}),
    "recovering": frozenset({"recording", "finalizing", "failed"}),
    "finalizing": frozenset({"complete", "partial", "failed"}),
}
ERROR_CODES = frozenset(
    {
        "capture_interrupted",
        "checksum_mismatch",
        "destination_rejected",
        "destination_unavailable",
        "disk_full",
        "finalization_failed",
        "process_crashed",
        "storage_io",
    }
)
ERROR_PHASES = frozenset({"capture", "delivery", "finalization", "recovery", "storage"})
DESTINATION_FINAL_STATUSES = frozenset(
    {"pending", "not_attempted", "accepted", "failed", "unknown"}
)
DESTINATION_ATTEMPT_STATUSES = frozenset({"in_progress", "accepted", "failed", "unknown"})
SOURCE_MODES = frozenset({"live", "filler", "unobserved"})
CLASSIFICATIONS = frozenset({"public-broadcast", "internal", "restricted"})

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")
_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$")
_STORAGE_KEY = re.compile(r"^artifacts/(?:segment-[0-9]{6}|final)\.[a-z0-9]{1,8}$")
_FORBIDDEN_KEY_PARTS = (
    "authorization",
    "credential",
    "message",
    "password",
    "rawerror",
    "secret",
    "streamkey",
    "token",
    "userid",
    "useridentifier",
    "url",
)


class RecordingContractError(ValueError):
    """A stable, safe reason for rejecting a recording manifest."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _fail(reason: str) -> None:
    raise RecordingContractError(reason)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _expect_object(value: object, keys: set[str], reason: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != keys:
        _fail(reason)
    return value


def _timestamp(value: object, reason: str) -> datetime:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        _fail(reason)
    try:
        return datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        _fail(reason)


def _reject_unsafe_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = re.sub(r"[^a-z]", "", str(key).lower())
            if any(part in normalized for part in _FORBIDDEN_KEY_PARTS):
                _fail("unsafe_field")
            _reject_unsafe_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_unsafe_keys(child)


def _validate_transitions(
    value: object, declared_state: str
) -> tuple[datetime, datetime | None, set[str], list[tuple[str, datetime]]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_TRANSITIONS:
        _fail("invalid_transitions")
    previous_state: str | None = None
    previous_at: datetime | None = None
    opened_at: datetime | None = None
    reason_codes: set[str] = set()
    causal_errors: list[tuple[str, datetime]] = []
    for index, raw in enumerate(value, 1):
        item = _expect_object(raw, {"sequence", "state", "at", "reasonCode"}, "invalid_transition")
        state = item["state"]
        at = _timestamp(item["at"], "invalid_transition_time")
        reason = item["reasonCode"]
        if not _is_int(item["sequence"]) or item["sequence"] != index or not isinstance(state, str) or state not in STATES:
            _fail("invalid_transition")
        if index == 1:
            if state != "pending":
                _fail("invalid_transition")
            opened_at = at
        elif state not in ALLOWED_TRANSITIONS.get(previous_state or "", frozenset()):
            _fail("invalid_transition")
        if previous_at is not None and at < previous_at:
            _fail("invalid_transition_time")
        if reason is not None and (not isinstance(reason, str) or reason not in ERROR_CODES):
            _fail("invalid_error_code")
        if reason is not None:
            reason_codes.add(reason)
            if state in {"interrupted", "failed"}:
                causal_errors.append((reason, at))
        if state in {"pending", "recording", "finalizing", "complete"} and reason is not None:
            _fail("unexpected_transition_reason")
        if state in {"interrupted", "recovering", "partial", "failed"} and reason is None:
            _fail("missing_transition_reason")
        previous_state = state
        previous_at = at
    if previous_state != declared_state or opened_at is None:
        _fail("state_mismatch")
    closed_at = previous_at if declared_state in TERMINAL_STATES else None
    return opened_at, closed_at, reason_codes, causal_errors


def _validate_artifact(raw: object, *, kind: str, sequence: int | None = None) -> dict[str, Any]:
    keys = {"kind", "storageKey", "durationMs", "byteLength", "sha256"}
    if kind == "segment":
        keys |= {"sequence", "startedOffsetMs"}
    item = _expect_object(raw, keys, "invalid_artifact")
    if item["kind"] != kind:
        _fail("invalid_artifact")
    if not isinstance(item["storageKey"], str) or not _STORAGE_KEY.fullmatch(item["storageKey"]):
        _fail("unsafe_storage_key")
    if not _is_int(item["durationMs"]) or not 1 <= item["durationMs"] <= 86_400_000:
        _fail("invalid_duration")
    if not _is_int(item["byteLength"]) or not 1 <= item["byteLength"] <= 1_099_511_627_776:
        _fail("invalid_byte_length")
    if not isinstance(item["sha256"], str) or not _SHA256.fullmatch(item["sha256"]):
        _fail("missing_or_invalid_checksum")
    if kind == "segment":
        expected_key = f"artifacts/segment-{sequence:06d}."
        if not _is_int(item["sequence"]) or item["sequence"] != sequence or not item["storageKey"].startswith(expected_key):
            _fail("invalid_segment_sequence")
        if not _is_int(item["startedOffsetMs"]) or item["startedOffsetMs"] < 0:
            _fail("invalid_segment_timing")
    elif not item["storageKey"].startswith("artifacts/final."):
        _fail("invalid_artifact")
    return item


def _validate_capture(value: object, state: str) -> list[dict[str, Any]]:
    capture = _expect_object(
        value,
        {
            "profile",
            "elapsedDurationMs",
            "capturedDurationMs",
            "droppedFrames",
            "sourceTimeline",
            "segments",
            "finalArtifact",
        },
        "invalid_capture",
    )
    profile = _expect_object(
        capture["profile"], {"container", "video", "audio", "timeBase"}, "invalid_media_profile"
    )
    if not isinstance(profile["container"], str) or not _MEDIA_NAME.fullmatch(profile["container"]):
        _fail("invalid_media_profile")
    video = profile["video"]
    if video is not None:
        video = _expect_object(video, {"codec", "width", "height", "frameRate"}, "invalid_media_profile")
        frame_rate = _expect_object(
            video["frameRate"], {"numerator", "denominator"}, "invalid_media_profile"
        )
        if (
            not isinstance(video["codec"], str)
            or not _MEDIA_NAME.fullmatch(video["codec"])
            or not _is_int(video["width"])
            or not 1 <= video["width"] <= 7680
            or not _is_int(video["height"])
            or not 1 <= video["height"] <= 4320
            or not _is_int(frame_rate["numerator"])
            or not 1 <= frame_rate["numerator"] <= 240_000
            or not _is_int(frame_rate["denominator"])
            or not 1 <= frame_rate["denominator"] <= 1001
        ):
            _fail("invalid_media_profile")
    audio = profile["audio"]
    if audio is not None:
        audio = _expect_object(audio, {"codec", "sampleRateHz", "channels"}, "invalid_media_profile")
        if (
            not isinstance(audio["codec"], str)
            or not _MEDIA_NAME.fullmatch(audio["codec"])
            or not _is_int(audio["sampleRateHz"])
            or not 8_000 <= audio["sampleRateHz"] <= 384_000
            or not _is_int(audio["channels"])
            or not 1 <= audio["channels"] <= 32
        ):
            _fail("invalid_media_profile")
    if video is None and audio is None:
        _fail("invalid_media_profile")
    if profile["timeBase"] != "milliseconds":
        _fail("invalid_media_profile")
    for key in ("elapsedDurationMs", "capturedDurationMs"):
        if not _is_int(capture[key]) or not 0 <= capture[key] <= MAX_COUNTER:
            _fail("invalid_duration")
    if not _is_int(capture["droppedFrames"]) or not 0 <= capture["droppedFrames"] <= MAX_COUNTER:
        _fail("invalid_dropped_frames")
    timeline = capture["sourceTimeline"]
    if not isinstance(timeline, list) or len(timeline) > MAX_SOURCE_EVENTS:
        _fail("invalid_source_timeline")
    previous_offset: int | None = None
    previous_mode: str | None = None
    for sequence, raw_event in enumerate(timeline, 1):
        event = _expect_object(
            raw_event, {"sequence", "startedOffsetMs", "mode"}, "invalid_source_event"
        )
        if (
            not _is_int(event["sequence"])
            or event["sequence"] != sequence
            or not isinstance(event["mode"], str)
            or event["mode"] not in SOURCE_MODES
            or not _is_int(event["startedOffsetMs"])
            or not 0 <= event["startedOffsetMs"] <= MAX_COUNTER
            or (previous_offset is not None and event["startedOffsetMs"] <= previous_offset)
            or event["mode"] == previous_mode
        ):
            _fail("invalid_source_event")
        previous_offset = event["startedOffsetMs"]
        previous_mode = event["mode"]
    if not isinstance(capture["segments"], list) or len(capture["segments"]) > MAX_SEGMENTS:
        _fail("invalid_segments")
    segments = [
        _validate_artifact(raw, kind="segment", sequence=index)
        for index, raw in enumerate(capture["segments"], 1)
    ]
    previous_end = 0
    for item in segments:
        if item["startedOffsetMs"] < previous_end:
            _fail("invalid_segment_timing")
        previous_end = item["startedOffsetMs"] + item["durationMs"]
    captured = sum(item["durationMs"] for item in segments)
    if capture["capturedDurationMs"] != captured or capture["elapsedDurationMs"] < previous_end:
        _fail("inconsistent_duration")
    if capture["elapsedDurationMs"] == 0:
        if timeline:
            _fail("invalid_source_timeline")
    elif not timeline or timeline[0]["startedOffsetMs"] != 0 or timeline[-1]["startedOffsetMs"] >= capture["elapsedDurationMs"]:
        _fail("invalid_source_timeline")
    source_intervals = []
    for index, event in enumerate(timeline):
        end = timeline[index + 1]["startedOffsetMs"] if index + 1 < len(timeline) else capture["elapsedDurationMs"]
        if event["mode"] != "unobserved":
            source_intervals.append((event["startedOffsetMs"], end))
    segment_intervals = [
        (item["startedOffsetMs"], item["startedOffsetMs"] + item["durationMs"])
        for item in segments
    ]
    if _merge_intervals(source_intervals) != _merge_intervals(segment_intervals):
        _fail("source_timeline_mismatch")
    final = capture["finalArtifact"]
    if final is not None:
        final = _validate_artifact(final, kind="final")
        if final["durationMs"] != capture["elapsedDurationMs"]:
            _fail("inconsistent_duration")
    if state == "complete" and (not segments or final is None):
        _fail("incomplete_capture")
    if state in STATES - TERMINAL_STATES and final is not None:
        _fail("active_capture_has_final_artifact")
    if state == "failed" and final is not None:
        _fail("failed_capture_has_final_artifact")
    if state == "pending" and (
        capture["elapsedDurationMs"]
        or capture["capturedDurationMs"]
        or capture["droppedFrames"]
        or timeline
        or segments
    ):
        _fail("pending_capture_has_progress")
    return [*segments, *([final] if final is not None else [])]


def _merge_intervals(intervals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in intervals:
        if merged and start == merged[-1][1]:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def _validate_destinations(
    value: object, state: str, opened_at: datetime, closed_at: datetime | None
) -> tuple[set[str], list[tuple[str, datetime]]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_DESTINATIONS:
        _fail("invalid_destinations")
    if state == "pending" and any(
        not isinstance(item, dict)
        or item.get("finalStatus") != "pending"
        or item.get("attempts")
        for item in value
    ):
        _fail("pending_delivery_has_progress")
    error_codes: set[str] = set()
    causal_errors: list[tuple[str, datetime]] = []
    for slot, raw in enumerate(value, 1):
        destination = _expect_object(raw, {"slot", "finalStatus", "attempts"}, "invalid_destination")
        if (
            not _is_int(destination["slot"])
            or destination["slot"] != slot
            or not isinstance(destination["finalStatus"], str)
            or destination["finalStatus"] not in DESTINATION_FINAL_STATUSES
        ):
            _fail("invalid_destination")
        attempts = destination["attempts"]
        if not isinstance(attempts, list) or len(attempts) > MAX_ATTEMPTS:
            _fail("invalid_destination_attempts")
        previous_ended: datetime | None = None
        for sequence, raw_attempt in enumerate(attempts, 1):
            attempt = _expect_object(
                raw_attempt,
                {"sequence", "startedAt", "endedAt", "status", "observedBytes", "errorCode"},
                "invalid_destination_attempt",
            )
            started = _timestamp(attempt["startedAt"], "invalid_destination_attempt_time")
            status = attempt["status"]
            ended = (
                None
                if attempt["endedAt"] is None
                else _timestamp(attempt["endedAt"], "invalid_destination_attempt_time")
            )
            if (
                not _is_int(attempt["sequence"])
                or attempt["sequence"] != sequence
                or not isinstance(status, str)
                or status not in DESTINATION_ATTEMPT_STATUSES
                or started < opened_at
                or (previous_ended is not None and started < previous_ended)
                or (closed_at is not None and started > closed_at)
            ):
                _fail("invalid_destination_attempt")
            if not _is_int(attempt["observedBytes"]) or not 0 <= attempt["observedBytes"] <= MAX_COUNTER:
                _fail("invalid_destination_attempt")
            if status == "in_progress":
                if ended is not None or attempt["errorCode"] is not None or sequence != len(attempts):
                    _fail("invalid_in_progress_attempt")
            elif ended is None or ended < started or (closed_at is not None and ended > closed_at):
                _fail("invalid_destination_attempt")
            elif status == "accepted":
                if attempt["errorCode"] is not None:
                    _fail("unexpected_destination_error")
                if attempt["observedBytes"] == 0:
                    _fail("missing_destination_bytes")
            elif attempt["errorCode"] not in ERROR_CODES:
                _fail("missing_or_invalid_destination_error")
            else:
                error_codes.add(attempt["errorCode"])
                causal_errors.append((attempt["errorCode"], ended))
            if ended is not None:
                previous_ended = ended
        if not attempts:
            expected_final_status = "not_attempted" if state in TERMINAL_STATES else "pending"
        elif attempts[-1]["status"] == "in_progress":
            expected_final_status = "pending"
        else:
            expected_final_status = attempts[-1]["status"]
        if destination["finalStatus"] != expected_final_status:
            _fail("destination_status_mismatch")
    if state == "complete" and any(item["finalStatus"] != "accepted" for item in value):
        _fail("incomplete_delivery")
    if state in TERMINAL_STATES and any(item["finalStatus"] == "pending" for item in value):
        _fail("incomplete_delivery")
    return error_codes, causal_errors


def _validate_errors(
    value: object, state: str, opened_at: datetime, closed_at: datetime | None
) -> dict[str, list[datetime]]:
    if not isinstance(value, list) or len(value) > MAX_ERRORS:
        _fail("invalid_errors")
    occurrences: dict[str, list[datetime]] = {}
    previous_at: datetime | None = None
    for sequence, raw in enumerate(value, 1):
        item = _expect_object(raw, {"sequence", "at", "phase", "code", "retryable"}, "invalid_error")
        if (
            not _is_int(item["sequence"])
            or item["sequence"] != sequence
            or not isinstance(item["phase"], str)
            or item["phase"] not in ERROR_PHASES
            or not isinstance(item["code"], str)
            or item["code"] not in ERROR_CODES
            or not isinstance(item["retryable"], bool)
        ):
            _fail("invalid_error")
        at = _timestamp(item["at"], "invalid_error_time")
        if (
            at < opened_at
            or (closed_at is not None and at > closed_at)
            or (previous_at is not None and at < previous_at)
        ):
            _fail("invalid_error_time")
        occurrences.setdefault(item["code"], []).append(at)
        previous_at = at
    if state in {"partial", "failed"} and not value:
        _fail("missing_terminal_error")
    if state == "pending" and value:
        _fail("pending_session_has_errors")
    return occurrences


def _validate_retention(
    value: object, opened_at: datetime, closed_at: datetime | None
) -> None:
    retention = _expect_object(value, {"classification", "deleteAfter", "legalHold"}, "invalid_retention")
    if (
        not isinstance(retention["classification"], str)
        or retention["classification"] not in CLASSIFICATIONS
        or not isinstance(retention["legalHold"], bool)
    ):
        _fail("invalid_retention")
    delete_after = retention["deleteAfter"]
    if retention["legalHold"]:
        if delete_after is not None:
            _fail("invalid_retention")
    elif delete_after is None or _timestamp(delete_after, "invalid_retention") <= (closed_at or opened_at):
        _fail("invalid_retention")


def _validate_error_causality(
    causes: list[tuple[str, datetime]], recorded: dict[str, list[datetime]]
) -> None:
    causes_by_code: dict[str, set[datetime]] = {}
    for code, occurred_at in causes:
        causes_by_code.setdefault(code, set()).add(occurred_at)
    for code, cause_times in causes_by_code.items():
        occurrences = recorded.get(code, [])
        occurrence_index = 0
        for cause_time in sorted(cause_times):
            while (
                occurrence_index < len(occurrences)
                and occurrences[occurrence_index] < cause_time
            ):
                occurrence_index += 1
            if occurrence_index == len(occurrences):
                _fail("noncausal_error_time")
            occurrence_index += 1


def _verify_artifacts(artifacts: list[dict[str, Any]], root: Path) -> None:
    root = root.resolve()
    for artifact in artifacts:
        path = (root / artifact["storageKey"]).resolve()
        if root not in path.parents:
            _fail("unsafe_storage_key")
        try:
            size = path.stat().st_size
        except OSError:
            _fail("artifact_unavailable")
        if size != artifact["byteLength"]:
            _fail("artifact_size_mismatch")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            _fail("artifact_unavailable")
        if digest.hexdigest() != artifact["sha256"]:
            _fail("artifact_checksum_mismatch")


def validate_manifest(value: object, *, artifact_root: Path | None = None) -> Mapping[str, Any]:
    """Validate one manifest and optionally verify its local artifact bytes."""

    _reject_unsafe_keys(value)
    manifest = _expect_object(
        value,
        {
            "contractVersion",
            "sessionId",
            "configurationHash",
            "state",
            "openedAt",
            "closedAt",
            "transitions",
            "capture",
            "destinations",
            "errors",
            "retention",
        },
        "invalid_manifest",
    )
    if manifest["contractVersion"] != CONTRACT_VERSION:
        _fail("unsupported_contract_version")
    if not isinstance(manifest["sessionId"], str) or not _UUID.fullmatch(manifest["sessionId"]):
        _fail("invalid_session_id")
    if not isinstance(manifest["configurationHash"], str) or not _SHA256.fullmatch(manifest["configurationHash"]):
        _fail("invalid_configuration_hash")
    state = manifest["state"]
    if not isinstance(state, str) or state not in STATES:
        _fail("invalid_state")
    opened_at, terminal_at, transition_errors, transition_causes = _validate_transitions(
        manifest["transitions"], state
    )
    if _timestamp(manifest["openedAt"], "invalid_opened_at") != opened_at:
        _fail("opened_at_mismatch")
    if state in TERMINAL_STATES:
        if _timestamp(manifest["closedAt"], "invalid_closed_at") != terminal_at:
            _fail("closed_at_mismatch")
    elif manifest["closedAt"] is not None:
        _fail("unexpected_closed_at")
    artifacts = _validate_capture(manifest["capture"], state)
    if terminal_at is not None:
        wall_duration_ms = int((terminal_at - opened_at).total_seconds() * 1_000)
        if manifest["capture"]["elapsedDurationMs"] > wall_duration_ms:
            _fail("inconsistent_duration")
    destination_errors, destination_causes = _validate_destinations(
        manifest["destinations"], state, opened_at, terminal_at
    )
    recorded_errors = _validate_errors(manifest["errors"], state, opened_at, terminal_at)
    if not (transition_errors | destination_errors) <= set(recorded_errors):
        _fail("missing_structured_error")
    _validate_error_causality(transition_causes + destination_causes, recorded_errors)
    _validate_retention(manifest["retention"], opened_at, terminal_at)
    if artifact_root is not None:
        _verify_artifacts(artifacts, artifact_root)
    return manifest


def load_manifest(path: Path, *, artifact_root: Path | None = None) -> Mapping[str, Any]:
    """Read and validate a UTF-8 JSON manifest."""

    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_fields
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise RecordingContractError("invalid_manifest_json") from exc
    return validate_manifest(value, artifact_root=artifact_root)


def _reject_duplicate_fields(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            _fail("duplicate_field")
        value[key] = child
    return value


def render_session_report(value: object) -> str:
    """Render a deterministic, bounded, credential-free operator summary."""

    manifest = validate_manifest(value)
    capture = manifest["capture"]
    destinations = manifest["destinations"]
    status_counts = Counter(item["finalStatus"] for item in destinations)
    error_counts = Counter(item["code"] for item in manifest["errors"])
    final = capture["finalArtifact"]
    total_bytes = sum(item["byteLength"] for item in capture["segments"])
    source_durations: Counter[str] = Counter()
    timeline = capture["sourceTimeline"]
    for index, event in enumerate(timeline):
        end = timeline[index + 1]["startedOffsetMs"] if index + 1 < len(timeline) else capture["elapsedDurationMs"]
        source_durations[event["mode"]] += end - event["startedOffsetMs"]
    errors = ",".join(f"{code}:{count}" for code, count in sorted(error_counts.items())) or "none"
    source_timeline = ",".join(
        f"{event['startedOffsetMs']}:{event['mode']}" for event in timeline
    ) or "none"
    retention = manifest["retention"]
    report = "\n".join(
        (
            "Croccante recording session report v1",
            f"session={manifest['sessionId']} state={manifest['state']}",
            f"opened={manifest['openedAt']} closed={manifest['closedAt'] or 'open'}",
            f"capture=segments:{len(capture['segments'])} captured_ms:{capture['capturedDurationMs']} elapsed_ms:{capture['elapsedDurationMs']} bytes:{total_bytes} dropped_frames:{capture['droppedFrames']} live_ms:{source_durations['live']} filler_ms:{source_durations['filler']} unobserved_ms:{source_durations['unobserved']}",
            f"source_timeline={source_timeline}",
            f"final={'present' if final is not None else 'absent'} duration_ms:{final['durationMs'] if final else 0} bytes:{final['byteLength'] if final else 0}",
            f"delivery=attempted:{sum(len(item['attempts']) for item in destinations)} in_flight:{sum(attempt['status'] == 'in_progress' for item in destinations for attempt in item['attempts'])} accepted:{status_counts['accepted']} failed:{status_counts['failed']} unknown:{status_counts['unknown']} not_attempted:{status_counts['not_attempted']} pending:{status_counts['pending']} observed_bytes:{sum(attempt['observedBytes'] for item in destinations for attempt in item['attempts'])}",
            f"errors={errors}",
            f"retention={retention['classification']} delete_after={retention['deleteAfter'] or 'legal-hold'} legal_hold={str(retention['legalHold']).lower()}",
        )
    ) + "\n"
    if len(report.encode("utf-8")) > MAX_REPORT_BYTES:
        _fail("report_too_large")
    return report
