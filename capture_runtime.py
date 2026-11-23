#!/usr/bin/env python3
"""Always-on as-aired capture for a Croccante session, driven by the lifecycle API.

`control-server.py` calls `start` and `stop` with the Start/Stop commands and `tick` once a
second with what the relay is doing. The session opens when the first media is on air, follows
the relay between the live input and the filler asset, records one destination attempt per slot,
and closes when Stop arrives. Nothing here raises into the caller: a recorder fault is counted and
delivery goes on. Only slot numbers are recorded for destinations, never a URL or a stream key.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from as_aired_capture import AsAiredCapture, CaptureSettings

GIB = 1024**3
# Storage bounds, like Alana's recording: a 50 GiB store, sessions kept 7 days (168 h). A session may
# use the whole store; opening the next one removes the oldest finished sessions first. At the relay's
# stream copy the store holds about 24 hours of a 4.6 Mbit/s program (about 2.1 GB per hour).
MAX_TOTAL_BYTES = 50 * GIB
MAX_SESSION_BYTES = 50 * GIB
RETENTION_DAYS = 7
SEGMENT_SECONDS = 6
CLASSIFICATION = "internal"
# Destinations the relay is putting media on: a slot in one of these states has an open attempt.
ON_AIR = frozenset({"relaying", "filler"})
# A capture process that keeps dying is restarted with a growing pause, up to this many seconds.
MAX_RECOVER_PAUSE_SECONDS = 60


def stream_profile(profile: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The declared video and audio facts of a prepared filler profile (the broadcast profile)."""

    video = {
        "codec": "h264",
        "width": int(profile["width"]),
        "height": int(profile["height"]),
        "frameRate": {"numerator": int(profile["fps"]), "denominator": 1},
    }
    audio = {"codec": "aac", "sampleRateHz": int(profile["audioRate"]), "channels": int(profile["audioChannels"])}
    return video, audio


class CaptureRuntime:
    """One as-aired capture per Start; no method raises."""

    def __init__(
        self,
        root: Path,
        *,
        capture_class: Callable[..., Any] = AsAiredCapture,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        close_in_background: bool = True,
        **capture_arguments: Any,
    ) -> None:
        self.root = root
        self._capture_class = capture_class
        self._clock = clock
        self._close_in_background = close_in_background
        self._capture_arguments = capture_arguments
        self._lock = threading.RLock()
        self._plan: dict[str, Any] | None = None
        self._capture: Any | None = None
        self._mode: str | None = None
        self._source: str | None = None
        self._recoveries = 0
        self._recover_after: datetime | None = None
        self._segments_at_recover = 0
        self._live_failures = 0
        self._open_slots: dict[int, int] = {}
        self._failures = 0
        self._last: dict[str, int | str] = {"state": "unavailable", "segments": 0, "bytes": 0}
        self._closers: list[threading.Thread] = []

    def guard(self, action: Callable[[], None]) -> None:
        """Run something that feeds the recorder; a fault is counted, never raised."""

        try:
            action()
        except Exception:  # noqa: BLE001 - the relay must not notice a recorder fault
            with self._lock:
                self._failures += 1

    # -- lifecycle ---------------------------------------------------------
    def start(self, *, profile: Mapping[str, Any], destination_count: int, configuration_hash: str) -> None:
        """Remember what the next session records; it opens when media first goes on air."""

        self.stop()
        with self._lock:
            try:
                self._plan = {"profile": stream_profile(profile), "count": destination_count, "hash": configuration_hash}
            except Exception:  # noqa: BLE001 - capture must never disturb Start
                self._failures += 1

    def stop(self) -> None:
        """Close the session. The finalization (joining segments) runs off the Stop request."""

        with self._lock:
            try:
                self._plan = None
                capture, slots = self._capture, self._open_slots
                self._capture, self._mode, self._source, self._open_slots = None, None, None, {}
                if capture is None:
                    return
                self._last = {**self._snapshot_of(capture)}
                self._failures += int(getattr(capture, "failures", 0))
                self._live_failures = 0
                self._recoveries, self._recover_after, self._segments_at_recover = 0, None, 0
                if self._close_in_background:
                    closer = threading.Thread(target=self._close, args=(capture, slots), name="as-aired-close")
                    self._closers = [item for item in self._closers if item.is_alive()] + [closer]
                    closer.start()
                else:
                    self._close(capture, slots)
            except Exception:  # noqa: BLE001
                self._failures += 1

    def wait(self, timeout: float = 30.0) -> None:
        """Wait for sessions that are still finalizing (tests and shutdown)."""

        for closer in list(self._closers):
            closer.join(timeout)

    def _close(self, capture: Any, slots: dict[int, int]) -> None:
        try:
            capture.close(before_finalize=lambda: self._end_attempts(capture, slots))
            snapshot = self._snapshot_of(capture)
            with self._lock:
                self._last = snapshot
        except Exception:  # noqa: BLE001
            with self._lock:
                self._failures += 1

    # -- once a second -----------------------------------------------------
    def tick(self, *, live_source: str | None, filler_source: str | None, destinations: list[str]) -> None:
        """Follow the relay: its source, its destinations, and the recorder's own segments."""

        with self._lock:
            try:
                mode = "live" if live_source else "filler" if filler_source else None
                source = live_source or filler_source
                if self._capture is None:
                    if self._plan is None or mode is None or source is None:
                        return
                    self._open(source, mode)
                capture = self._capture
                if mode is not None and source is not None and source != self._source:
                    # A new input under the same mode (another publisher, another filler version) is
                    # followed too, not only a change between live and filler.
                    if mode != self._mode:
                        capture.note_source(mode)
                    capture.retarget(source)
                    self._mode, self._source = mode, source
                capture.poll()  # first, so an attempt that ends now is counted against the freshest bytes
                self._follow_destinations(capture, destinations)
                self._recover(capture)
                self._last = self._snapshot_of(capture)
                self._live_failures = int(getattr(capture, "failures", 0))
            except Exception:  # noqa: BLE001
                self._failures += 1

    def _recover(self, capture: Any) -> None:
        """Restart a dead capture process, pausing longer after each restart that records nothing."""

        snapshot = self._snapshot_of(capture)
        if int(snapshot["segments"]) > self._segments_at_recover:
            self._recoveries, self._segments_at_recover = 0, int(snapshot["segments"])
        now = self._clock()
        if snapshot["state"] != "interrupted" or (self._recover_after is not None and now < self._recover_after):
            return
        capture.recover()
        self._recoveries += 1
        pause = min(2 ** (self._recoveries - 1), MAX_RECOVER_PAUSE_SECONDS)
        self._recover_after = now + timedelta(seconds=pause)

    def _open(self, source: str, mode: str) -> None:
        assert self._plan is not None
        video, audio = self._plan["profile"]
        settings = CaptureSettings.from_mapping(
            {
                "source": source,
                "segmentSeconds": SEGMENT_SECONDS,
                "maxSessionBytes": MAX_SESSION_BYTES,
                "maxTotalBytes": MAX_TOTAL_BYTES,
                "retentionDays": RETENTION_DAYS,
                "classification": CLASSIFICATION,
                "video": video,
                "audio": audio,
            },
            root=self.root,
        )
        self.root.mkdir(parents=True, exist_ok=True)
        self._capture = self._capture_class(
            settings,
            destination_count=self._plan["count"],
            configuration_hash=self._plan["hash"],
            clock=self._clock,
            **self._capture_arguments,
        )
        self._mode, self._source = mode, source
        self._capture.note_source(mode)

    def _bytes(self, capture: Any) -> int:
        return int(capture.snapshot()["bytes"])

    def _follow_destinations(self, capture: Any, destinations: list[str]) -> None:
        for slot, state in enumerate(destinations, start=1):
            opened = slot in self._open_slots
            if state in ON_AIR and not opened:
                capture.destination_started(slot)
                self._open_slots[slot] = self._bytes(capture)
            elif state == "backoff" and opened:
                observed = max(0, self._bytes(capture) - self._open_slots.pop(slot))
                capture.destination_ended(slot, "failed", observed, "destination_unavailable")
            elif state not in ON_AIR and opened:
                # The slot left the air without failing (stopped, waiting): the attempt ends here.
                self._end_attempts(capture, {slot: self._open_slots.pop(slot)})

    def _end_attempts(self, capture: Any, slots: dict[int, int]) -> None:
        """Stop ends each open attempt against the final byte count: accepted when the relay put
        bytes on air during it, unknown otherwise."""

        total = self._bytes(capture)
        for slot, began in sorted(slots.items()):
            observed = total - began
            if observed > 0:
                capture.destination_ended(slot, "accepted", observed, None)
            else:
                capture.destination_ended(slot, "unknown", 0, "destination_unavailable")

    # -- observability -----------------------------------------------------
    @staticmethod
    def _snapshot_of(capture: Any) -> dict[str, int | str]:
        snapshot = capture.snapshot()
        return {"state": str(snapshot["state"]), "segments": int(snapshot["segments"]), "bytes": int(snapshot["bytes"])}

    def snapshot(self) -> dict[str, int | str]:
        """Bounded facts for metrics, from what the last tick saw: it never waits for the recorder."""

        return {**self._last, "recorder_failures": self._failures + self._live_failures}
