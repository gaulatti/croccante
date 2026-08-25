"""Atomic runtime ownership for resolved destination supervisors."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from destination_store import DestinationError, ResolvedSelection


class DestinationRuntime:
    def __init__(self, state_dir: Path, supervisor: str = "/usr/local/bin/relay-dest.sh") -> None:
        self.state_dir = state_dir
        self.supervisor = supervisor
        self.processes: list[subprocess.Popen[bytes]] = []
        self.active: ResolvedSelection | None = None

    def public_ids(self) -> list[str]:
        if self.active is None:
            return []
        return [item.destination_id for item in self.active.destinations]

    def _write_private(self, path: Path, value: str) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(f"{value}\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)

    def _remove_runtime_files(self) -> None:
        for pattern in ("dest-*.url", "dest-*.id", "dest-*.pid", "dest-*.wrapper.pid", "dest-*.ffmpeg.pid", "dest-*.state"):
            for path in self.state_dir.glob(pattern):
                path.unlink(missing_ok=True)
        self._write_private(self.state_dir / "dest.count", "0")

    def deactivate(self) -> None:
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 5
        for process in self.processes:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        self.processes = []
        self.active = None
        self._remove_runtime_files()

    def activate(self, plan: ResolvedSelection) -> None:
        urls = [item.url for item in plan.destinations]
        if len(set(urls)) != len(urls):
            raise DestinationError("duplicate_resolved_destination")
        self.deactivate()
        started: list[subprocess.Popen[bytes]] = []
        try:
            for index, destination in enumerate(plan.destinations, start=1):
                self._write_private(self.state_dir / f"dest-{index}.url", destination.url)
                self._write_private(self.state_dir / f"dest-{index}.id", destination.destination_id)
                process = subprocess.Popen([self.supervisor, str(index)])
                started.append(process)
                self._write_private(self.state_dir / f"dest-{index}.wrapper.pid", str(process.pid))
            # Give every exec boundary enough time to fail fast (missing
            # binary, permissions, malformed runtime state) before committing
            # the set as active. This is one bounded wait for the whole set,
            # not one delay per destination.
            time.sleep(0.5)
            if any(process.poll() is not None for process in started):
                raise DestinationError("supervisor_start_failed")
            self.processes = started
            self.active = plan
            self._write_private(self.state_dir / "dest.count", str(len(started)))
        except Exception as exc:
            self.processes = started
            self.deactivate()
            if isinstance(exc, DestinationError):
                raise
            raise DestinationError("supervisor_start_failed") from exc
