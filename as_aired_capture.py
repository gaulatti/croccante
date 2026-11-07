#!/usr/bin/env python3
"""Opt-in as-aired capture of Croccante's final relay/filler output.

This module is the recorder runtime for the v1 recording contract
(`recording_manifest.py`). It is deliberately separate from Alana's clean
program master: it records what Croccante actually put on the air, including
filler intervals and gaps, plus a redacted delivery report.

Isolation is the central rule. `AsAiredCapture` never raises into its caller:
any recorder or report failure is counted, the session is closed as `failed`
or `partial`, and the relay keeps running. Nothing here reads, stores, or
emits a destination URL or stream key; the only URL handed to the recorder is
the local source to capture, and it is used only as an ffmpeg argument.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from recording_manifest import (
    CLASSIFICATIONS,
    CONTRACT_VERSION,
    MAX_ATTEMPTS,
    MAX_DESTINATIONS,
    MAX_ERRORS,
    MAX_SEGMENTS,
    MAX_SOURCE_EVENTS,
    MAX_TRANSITIONS,
    SOURCE_MODES,
    load_manifest,
    render_session_report,
    validate_manifest,
)

MIN_SEGMENT_SECONDS = 2
MAX_SEGMENT_SECONDS = 60
MAX_RETENTION_DAYS = 365
MAX_QUOTA_BYTES = 1_099_511_627_776
CONTAINER = "mpegts"
EXTENSION = "ts"
_TERMINAL = frozenset({"complete", "partial", "failed"})
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
# Evidence headroom kept free so a saturated session can still close validly.
_RESERVE = 4


class CaptureConfigError(ValueError):
    """The opt-in capture settings are missing, ambiguous, or out of bounds."""


@dataclass(frozen=True)
class CaptureSettings:
    """Explicit opt-in settings. There is no default-enabled instance."""

    root: Path
    source: str
    segment_seconds: int
    max_session_bytes: int
    max_total_bytes: int
    retention_days: int
    classification: str
    video: dict[str, Any] | None
    audio: dict[str, Any] | None
    legal_hold: bool = False

    @staticmethod
    def from_mapping(raw: Mapping[str, Any] | None, *, root: Path) -> "CaptureSettings | None":
        """Return None unless capture was explicitly enabled; reject bad values."""

        if raw is None or raw.get("enabled") is not True:
            return None
        allowed = {
            "enabled", "source", "segmentSeconds", "maxSessionBytes", "maxTotalBytes",
            "retentionDays", "classification", "legalHold", "video", "audio",
        }
        if set(raw) - allowed:
            raise CaptureConfigError("unknown_setting")

        def integer(name: str, low: int, high: int) -> int:
            value = raw.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise CaptureConfigError(f"invalid_{name}")
            return value

        source = raw.get("source")
        if not isinstance(source, str) or not _local_source(source):
            raise CaptureConfigError("invalid_source")
        classification = raw.get("classification")
        if not isinstance(classification, str) or classification not in CLASSIFICATIONS:
            raise CaptureConfigError("invalid_classification")
        video, audio = raw.get("video"), raw.get("audio")
        if not all(item is None or isinstance(item, dict) for item in (video, audio)) or (video is None and audio is None):
            raise CaptureConfigError("invalid_profile")
        legal_hold = raw.get("legalHold", False)
        if not isinstance(legal_hold, bool):
            raise CaptureConfigError("invalid_legalHold")
        settings = CaptureSettings(
            root=root,
            source=source,
            segment_seconds=integer("segmentSeconds", MIN_SEGMENT_SECONDS, MAX_SEGMENT_SECONDS),
            max_session_bytes=integer("maxSessionBytes", 1, MAX_QUOTA_BYTES),
            max_total_bytes=integer("maxTotalBytes", 1, MAX_QUOTA_BYTES),
            retention_days=integer("retentionDays", 1, MAX_RETENTION_DAYS),
            classification=classification,
            video=video,
            audio=audio,
            legal_hold=legal_hold,
        )
        if settings.max_session_bytes > settings.max_total_bytes:
            raise CaptureConfigError("invalid_maxSessionBytes")
        return settings


def _local_source(source: str) -> bool:
    """An absolute local path, or a credential-free loopback rtmp:// URL."""

    if source.startswith("/"):
        return "://" not in source
    parts = urlsplit(source)
    return parts.scheme == "rtmp" and parts.hostname in _LOOPBACK and parts.username is None and parts.password is None


def build_capture_command(settings: CaptureSettings, session_dir: Path, start_number: int) -> list[str]:
    """ffmpeg arguments for the capture leg: stream copy into numbered segments."""

    return [
        "ffmpeg", "-nostdin", "-loglevel", "error", "-i", settings.source,
        "-map", "0", "-c", "copy", "-f", "segment",
        "-segment_time", str(settings.segment_seconds),
        "-segment_format", CONTAINER, "-segment_start_number", str(start_number),
        "-reset_timestamps", "1",
        str(session_dir / "artifacts" / f"segment-%06d.{EXTENSION}"),
    ]


def build_probe_command(path: Path) -> list[str]:
    return [
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_entries", "stream=codec_type,codec_name,width,height,avg_frame_rate,sample_rate,channels:format=duration",
        str(path),
    ]


def parse_probe(output: str) -> dict[str, Any]:
    """Reduce ffprobe JSON to the contract's bounded media facts."""

    document = json.loads(output)
    duration_ms = round(float(document["format"]["duration"]) * 1000)
    video = audio = None
    for stream in document["streams"]:
        if stream["codec_type"] == "video" and video is None:
            numerator, denominator = (int(part) for part in stream["avg_frame_rate"].split("/"))
            video = {
                "codec": stream["codec_name"], "width": stream["width"], "height": stream["height"],
                "frameRate": {"numerator": numerator, "denominator": denominator},
            }
        elif stream["codec_type"] == "audio" and audio is None:
            audio = {"codec": stream["codec_name"], "sampleRateHz": int(stream["sample_rate"]), "channels": stream["channels"]}
    return {"durationMs": duration_ms, "video": video, "audio": audio}


def default_probe(path: Path) -> dict[str, Any]:
    result = subprocess.run(build_probe_command(path), capture_output=True, text=True, timeout=30, check=True)
    return parse_probe(result.stdout)


def default_start(command: list[str]) -> subprocess.Popen:
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _iso(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def derive_source_timeline(
    noted: list[tuple[int, str]], segments: list[dict[str, Any]], elapsed_ms: int
) -> list[dict[str, Any]]:
    """Source timeline that agrees with the captured segments.

    A noted `live`/`filler` mode only applies while a segment covers the time;
    uncovered time (before the first segment, between segments, or after the
    last) is `unobserved`, which is how gaps stay visible.
    """

    if elapsed_ms == 0:
        return []
    covered = [(s["startedOffsetMs"], s["startedOffsetMs"] + s["durationMs"]) for s in segments]
    points = {0, elapsed_ms, *(offset for offset, _ in noted if offset < elapsed_ms)}
    for start, end in covered:
        points.update((start, end))
    ordered = sorted(point for point in points if point <= elapsed_ms)
    timeline: list[dict[str, Any]] = []
    for start, end in zip(ordered, ordered[1:]):
        in_segment = any(low <= start and end <= high for low, high in covered)
        mode = "unobserved"
        if in_segment:
            for offset, noted_mode in noted:
                if offset <= start:
                    mode = noted_mode
        if not timeline or timeline[-1]["mode"] != mode:
            timeline.append({"sequence": len(timeline) + 1, "startedOffsetMs": start, "mode": mode})
    return timeline


class AsAiredSession:
    """One recording session; builds only manifests the v1 validator accepts."""

    def __init__(
        self,
        settings: CaptureSettings,
        *,
        destination_count: int,
        configuration_hash: str,
        clock: Callable[[], datetime],
        session_id: str | None = None,
    ) -> None:
        if not 1 <= destination_count <= MAX_DESTINATIONS:
            raise CaptureConfigError("invalid_destination_count")
        self.settings = settings
        self.session_id = session_id or str(uuid.uuid4())
        self.directory = settings.root / self.session_id
        self._clock = clock
        self._configuration_hash = configuration_hash
        self._opened = self._now()
        self._transitions: list[dict[str, Any]] = [{"sequence": 1, "state": "pending", "at": _iso(self._opened), "reasonCode": None}]
        self._state = "pending"
        self._recording_started: datetime | None = None
        self._segments: list[dict[str, Any]] = []
        self._noted: list[tuple[int, str]] = []
        self._destinations: list[list[dict[str, Any]]] = [[] for _ in range(destination_count)]
        self._errors: list[dict[str, Any]] = []
        self._final: dict[str, Any] | None = None
        self._profile: dict[str, Any] = {"container": CONTAINER, "video": settings.video, "audio": settings.audio, "timeBase": "milliseconds"}
        self._next_offset = 0
        self._elapsed_base = 0
        self._elapsed_at_close = 0
        self._interrupted_at: datetime | None = None
        self._gap_ms = 0
        self._interruptions = 0
        self.truncated: str | None = None
        self._cursor = 1
        (self.directory / "artifacts").mkdir(parents=True)
        self.persist()

    # -- clock and offsets -------------------------------------------------
    def _now(self) -> datetime:
        moment = self._clock().astimezone(timezone.utc)
        return moment.replace(microsecond=moment.microsecond // 1000 * 1000)

    def _offset_ms(self) -> int:
        if self._recording_started is None:
            return 0
        return max(self._elapsed_base, self._elapsed_base + int((self._now() - self._recording_started).total_seconds() * 1000))

    def _move(self, state: str, reason: str | None = None) -> None:
        self._transitions.append({"sequence": len(self._transitions) + 1, "state": state, "at": _iso(self._now()), "reasonCode": reason})
        self._state = state

    @property
    def state(self) -> str:
        return self._state

    @property
    def terminal(self) -> bool:
        return self._state in _TERMINAL

    @property
    def segment_count(self) -> int:
        return len(self._segments)

    @property
    def next_file_number(self) -> int:
        """Number of the next capture file to read (ffmpeg numbering, which can skip)."""

        return self._cursor

    @property
    def saturated(self) -> bool:
        """True when contract evidence bounds leave no room for more history."""

        return (
            len(self._errors) >= MAX_ERRORS - _RESERVE
            or len(self._transitions) >= MAX_TRANSITIONS - _RESERVE
            or any(len(attempts) >= MAX_ATTEMPTS - 1 for attempts in self._destinations)
            or len(self._noted) >= MAX_SOURCE_EVENTS - _RESERVE
            or len(derive_source_timeline(self._noted, self._segments, max(self._offset_ms(), self._next_offset))) >= MAX_SOURCE_EVENTS - _RESERVE
        )

    def saturate(self) -> None:
        """Stop recording more history: the rest of the broadcast is unobserved."""

        self.truncated = self.truncated or "capture_interrupted"
        if not any(error["code"] == "capture_interrupted" for error in self._errors):
            self.record_error("capture", "capture_interrupted", False)

    @property
    def total_bytes(self) -> int:
        return sum(segment["byteLength"] for segment in self._segments)

    # -- recorded facts ----------------------------------------------------
    def record_error(self, phase: str, code: str, retryable: bool) -> None:
        self._errors.append({"sequence": len(self._errors) + 1, "at": _iso(self._now()), "phase": phase, "code": code, "retryable": retryable})

    def start_recording(self) -> None:
        self._move("recording")
        self._recording_started = self._now()
        self._noted.append((0, "live"))  # the relay is live until a filler transition is noted

    def note_source(self, mode: str) -> None:
        """Record a relay/filler transition at the current session offset."""

        if mode not in SOURCE_MODES - {"unobserved"}:
            raise CaptureConfigError("invalid_source_mode")
        offset = self._offset_ms()
        if self._noted and self._noted[-1][0] == offset:
            self._noted[-1] = (offset, mode)
        else:
            self._noted.append((offset, mode))

    def begin_destination_attempt(self, slot: int) -> None:
        attempts = self._destinations[slot - 1]
        attempts.append({"sequence": len(attempts) + 1, "startedAt": _iso(self._now()), "endedAt": None, "status": "in_progress", "observedBytes": 0, "errorCode": None})

    def end_destination_attempt(self, slot: int, status: str, observed_bytes: int, error_code: str | None) -> None:
        attempt = self._destinations[slot - 1][-1]
        code = None if status == "accepted" else error_code or "destination_unavailable"
        attempt.update(endedAt=_iso(self._now()), status=status, observedBytes=observed_bytes, errorCode=code)
        if code is not None:
            self.record_error("delivery", code, status == "failed")

    def add_segment(self, path: Path, probe: Callable[[Path], dict[str, Any]]) -> bool:
        """Consume one finished capture file.

        Returns True when the file was handled and capture may continue: either
        registered, or (when it cannot be probed) discarded as a visible gap.
        Returns False, after discarding the file, when the per-session quota is
        reached or the measured codecs differ from the declared profile; the
        session then stops growing and ends `partial`.
        """

        self._cursor += 1
        size = path.stat().st_size
        if self.total_bytes + size > self.settings.max_session_bytes or len(self._segments) >= MAX_SEGMENTS:
            path.unlink()
            self.truncated = "disk_full"
            self.record_error("storage", "disk_full", False)
            return False
        try:
            facts = probe(path)
        except Exception:  # noqa: BLE001 - an unverifiable segment is a gap, not a reason to stop recording
            path.unlink()
            self._interruptions += 1
            self._next_offset += self.settings.segment_seconds * 1000
            self.record_error("capture", "capture_interrupted", True)
            return True
        if facts["video"] != self._profile["video"] or facts["audio"] != self._profile["audio"]:
            path.unlink()
            self.truncated = "capture_interrupted"
            self.record_error("capture", "capture_interrupted", False)
            return False
        sequence = len(self._segments) + 1
        target = path.with_name(f"segment-{sequence:06d}.{EXTENSION}")
        if target != path:
            os.replace(path, target)
        self._segments.append({
            "kind": "segment", "sequence": sequence, "storageKey": f"artifacts/{target.name}",
            "startedOffsetMs": self._next_offset, "durationMs": facts["durationMs"], "byteLength": size, "sha256": _sha256(target),
        })
        self._next_offset += facts["durationMs"]
        return True

    def interrupt(self, code: str) -> None:
        self._interrupted_at = self._now()
        self._interruptions += 1
        self.record_error("capture", code, True)
        self._move("interrupted", code)

    def resume(self, code: str) -> None:
        """interrupted -> recovering -> recording; the gap stays `unobserved`."""

        self._move("recovering", code)
        self._move("recording")
        now = self._now()
        missed = int((now - self._interrupted_at).total_seconds() * 1000)
        self._gap_ms += missed
        self._next_offset += missed
        self._elapsed_base = self._next_offset
        self._recording_started = now

    # -- manifest ----------------------------------------------------------
    def _retention(self) -> dict[str, Any]:
        anchor = datetime.strptime(self._transitions[-1]["at"], "%Y-%m-%dT%H:%M:%S.%fZ") if self.terminal else self._opened
        delete_after = None if self.settings.legal_hold else _iso(anchor.replace(tzinfo=timezone.utc) + timedelta(days=self.settings.retention_days))
        return {"classification": self.settings.classification, "deleteAfter": delete_after, "legalHold": self.settings.legal_hold}

    def manifest(self) -> dict[str, Any]:
        elapsed = self._elapsed_at_close if self.terminal else max(self._offset_ms(), self._next_offset)
        destinations = []
        for index, attempts in enumerate(self._destinations, 1):
            if not attempts:
                final = "not_attempted" if self.terminal else "pending"
            else:
                final = "pending" if attempts[-1]["status"] == "in_progress" else attempts[-1]["status"]
            destinations.append({"slot": index, "finalStatus": final, "attempts": [dict(item) for item in attempts]})
        return {
            "contractVersion": CONTRACT_VERSION,
            "sessionId": self.session_id,
            "configurationHash": self._configuration_hash,
            "state": self._state,
            "openedAt": _iso(self._opened),
            "closedAt": self._transitions[-1]["at"] if self.terminal else None,
            "transitions": [dict(item) for item in self._transitions],
            "capture": {
                "profile": {**self._profile},
                "elapsedDurationMs": elapsed,
                "capturedDurationMs": sum(segment["durationMs"] for segment in self._segments),
                "droppedFrames": 0,
                "sourceTimeline": derive_source_timeline(self._noted, self._segments, elapsed),
                "segments": [dict(segment) for segment in self._segments],
                "finalArtifact": self._final,
            },
            "destinations": destinations,
            "errors": [dict(error) for error in self._errors],
            "retention": self._retention(),
        }

    def persist(self) -> None:
        """Validate before writing so an invalid state is never published."""

        manifest = self.manifest()
        validate_manifest(manifest)
        _atomic_write(self.directory / "manifest.json", json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        if self.terminal:
            _atomic_write(self.directory / "report.txt", render_session_report(manifest))

    # -- close -------------------------------------------------------------
    def fail(self, code: str, phase: str) -> None:
        """Terminate with `failed` (no final artifact is allowed)."""

        self._elapsed_at_close = self._next_offset
        self.record_error(phase, code, False)
        self._move("failed", code)
        self._final = None
        self._close_open_attempts()
        self.persist()

    def _close_open_attempts(self) -> None:
        """Unresolved attempts become `unknown`, recorded once to stay within bounds."""

        closed = False
        for attempts in self._destinations:
            if attempts and attempts[-1]["status"] == "in_progress":
                attempts[-1].update(endedAt=_iso(self._now()), status="unknown", errorCode="finalization_failed")
                closed = True
        if closed:
            self.record_error("delivery", "finalization_failed", False)

    def finalize(self) -> None:
        """Concatenate verified segments into one artifact, then close the session."""

        self._elapsed_at_close = self._next_offset
        self._move("finalizing")
        self._close_open_attempts()
        problem: str | None = None
        if self._interruptions or not self._segments:
            problem = "capture_interrupted"
            if not any(error["code"] == problem for error in self._errors):
                self.record_error("capture", problem, False)
        else:
            try:
                self._final = self._concatenate()
            except OSError:
                problem = "finalization_failed"
                self.record_error("finalization", problem, False)
        failed_delivery = any(attempts and attempts[-1]["status"] != "accepted" for attempts in self._destinations)
        undelivered = any(not attempts for attempts in self._destinations)
        reason = problem or self.truncated or ("destination_unavailable" if failed_delivery or undelivered else None)
        if reason is None:
            self._move("complete")
        else:
            if not any(error["code"] == reason for error in self._errors):
                self.record_error("delivery", reason, False)  # only delivery reasons are not already recorded
            self._move("partial", reason)
        self.persist()

    def _concatenate(self) -> dict[str, Any]:
        target = self.directory / "artifacts" / f"final.{EXTENSION}"
        digest = hashlib.sha256()
        with target.open("wb") as output:
            for segment in self._segments:
                with (self.directory / segment["storageKey"]).open("rb") as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        digest.update(chunk)
                        output.write(chunk)
        size = target.stat().st_size
        if size != self.total_bytes or digest.hexdigest() != _sha256(target):
            raise OSError("final artifact verification failed")
        return {"kind": "final", "storageKey": f"artifacts/final.{EXTENSION}", "durationMs": self._elapsed_at_close, "byteLength": size, "sha256": digest.hexdigest()}


class AsAiredCapture:
    """Failure-isolated facade used by the relay. No method ever raises."""

    def __init__(
        self,
        settings: CaptureSettings,
        *,
        destination_count: int,
        configuration_hash: str,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        start: Callable[[list[str]], Any] = default_start,
        probe: Callable[[Path], dict[str, Any]] = default_probe,
        session_id: str | None = None,
    ) -> None:
        self.settings = settings
        self.failures = 0
        self.session: AsAiredSession | None = None
        self._start, self._probe, self._process = start, probe, None
        self._stopped = False
        self._clock = clock
        self._args = (destination_count, configuration_hash, session_id)
        self._guard(self._open)

    def _guard(self, action: Callable[[], None]) -> bool:
        try:
            action()
            return True
        except Exception:  # noqa: BLE001 - the recorder must never disturb the relay
            self.failures += 1
            return False

    def _open(self) -> None:
        count, configuration_hash, session_id = self._args
        # A retention fault must not prevent the session; the quota check below still applies.
        self._guard(lambda: apply_retention(self.settings.root, self._clock(), self.settings.max_total_bytes))
        self.session = AsAiredSession(self.settings, destination_count=count, configuration_hash=configuration_hash, clock=self._clock, session_id=session_id)
        if usage(self.settings.root) > self.settings.max_total_bytes:
            self.session.fail("disk_full", "storage")
            return
        self.session.start_recording()
        self._launch()
        self.session.persist()

    def _launch(self) -> None:
        assert self.session is not None
        command = build_capture_command(self.settings, self.session.directory, self.session.next_file_number)
        self._process = self._start(command)

    def _alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _active(self) -> bool:
        return self.session is not None and not self.session.terminal

    def _record(self, action: Callable[[], None]) -> None:
        """Run a guarded mutation, then close the session if evidence bounds are reached."""

        if self._active() and self._guard(action) and self.session.saturated:
            self.session.saturate()
            self.close()

    def note_source(self, mode: str) -> None:
        """Called on every relay <-> filler transition."""

        self._record(lambda: self.session.note_source(mode))

    def destination_started(self, slot: int) -> None:
        self._record(lambda: self.session.begin_destination_attempt(slot))

    def destination_ended(self, slot: int, status: str, observed_bytes: int, error_code: str | None) -> None:
        self._record(lambda: self.session.end_destination_attempt(slot, status, observed_bytes, error_code))

    def poll(self) -> None:
        """Register finished segments, detect a crashed recorder, publish the manifest."""

        self._record(self._poll)

    def _poll(self) -> None:
        session = self.session
        assert session is not None
        directory = session.directory / "artifacts"
        while True:
            number = session.next_file_number
            current = directory / f"segment-{number:06d}.{EXTENSION}"
            following = directory / f"segment-{number + 1:06d}.{EXTENSION}"
            if not current.exists() or not (following.exists() or not self._alive()):
                break
            if not session.add_segment(current, self._probe):
                self._stop_process()
                break
        if not self._alive() and not self._stopped and session.state == "recording":
            session.interrupt("process_crashed")
            self._process = None
        session.persist()

    def recover(self) -> None:
        """Restart the capture process after a crash; the outage stays a visible gap."""

        if self._active() and self.session.state == "interrupted":
            def action() -> None:
                self.session.resume("process_crashed")
                self._launch()
            self._record(action)

    def _stop_process(self) -> None:
        self._stopped = True
        if self._process is not None and self._alive():
            self._process.terminate()
            self._process.wait(timeout=10)

    def close(self) -> None:
        """Stop capture and finalize. Failure leaves a failed/partial manifest, never an exception."""

        if not self._active():
            return
        if not self._guard(self._close):
            self._guard(lambda: self.session.fail("finalization_failed", "finalization"))

    def _close(self) -> None:
        assert self.session is not None
        self._stop_process()
        self._poll_after_stop()
        self.session.finalize()

    def _poll_after_stop(self) -> None:
        session = self.session
        assert session is not None
        directory = session.directory / "artifacts"
        while True:
            current = directory / f"segment-{session.next_file_number:06d}.{EXTENSION}"
            if not current.exists() or not session.add_segment(current, self._probe):
                break

    def snapshot(self) -> dict[str, int | str]:
        """Bounded observability: counts and a closed state vocabulary only."""

        session = self.session
        return {
            "state": session.state if session else "unavailable",
            "segments": session.segment_count if session else 0,
            "bytes": session.total_bytes if session else 0,
            "recorder_failures": self.failures,
        }


def usage(root: Path) -> int:
    """Bytes held by capture sessions under `root`."""

    total = 0
    for path in root.glob("*/artifacts/*"):
        total += path.stat().st_size
    return total


def apply_retention(root: Path, now: datetime, max_total_bytes: int) -> dict[str, int]:
    """Delete expired terminal sessions, then oldest terminal sessions over quota.

    Only directories named by a v4 UUID that hold a valid terminal manifest are
    eligible; legal holds and non-terminal or unreadable sessions are never
    deleted.
    """

    result = {"expired": 0, "quota": 0, "kept": 0}
    if not root.is_dir():
        return result
    candidates = []
    for directory in sorted(root.iterdir()):
        try:
            uuid.UUID(directory.name, version=4)
            manifest = load_manifest(directory / "manifest.json")
        except (ValueError, OSError):
            continue
        if manifest["state"] not in _TERMINAL or manifest["retention"]["legalHold"]:
            continue
        candidates.append((manifest["closedAt"], directory, manifest["retention"]["deleteAfter"]))
    current = _iso(now)
    remaining = []
    for closed_at, directory, delete_after in sorted(candidates):
        if delete_after <= current:
            shutil.rmtree(directory)
            result["expired"] += 1
        else:
            remaining.append(directory)
    for directory in remaining:
        if usage(root) <= max_total_bytes:
            break
        shutil.rmtree(directory)
        result["quota"] += 1
    result["kept"] = sum(1 for _ in root.iterdir())
    return result
