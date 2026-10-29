#!/usr/bin/env python3
"""Process-source and parsing boundaries of the relay metrics collector."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("PROGRAM_ID", "bounded-test-program")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import relay_metrics

STAT_PATH = Path("/proc/1/stat")
STATUS_PATH = Path("/proc/1/status")
FD_PATH = Path("/proc/1/fd")


def fake_proc(stat: str, status: str):
    def read(path: Path, default: str = "") -> str:
        if path == STAT_PATH:
            return stat
        if path == STATUS_PATH:
            return status
        return default

    return read


def stat_line(user: str = "300", system: str = "200") -> str:
    fields = ["1", "(init)", "S"] + ["0"] * 10 + [user, system] + ["0"] * 10
    return " ".join(fields)


def process_samples(stat: str, status: str, ticks: int = 100, descriptors: object = 4) -> dict[str, float]:
    def iterdir(path: Path):
        if path != FD_PATH:
            raise AssertionError(path)
        if isinstance(descriptors, Exception):
            raise descriptors
        return iter([Path(str(index)) for index in range(descriptors)])  # type: ignore[call-overload]

    lines: list[str] = []
    with (
        patch.object(relay_metrics, "_read_text", fake_proc(stat, status)),
        patch.object(relay_metrics.os, "sysconf", return_value=ticks),
        patch.object(Path, "iterdir", iterdir),
    ):
        relay_metrics._process_metrics(lines)
    return {
        line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
        for line in lines
        if not line.startswith("#")
    }


class ProcessMetricsTest(unittest.TestCase):
    def test_reads_cpu_memory_and_descriptors_from_pid_one(self) -> None:
        samples = process_samples(stat_line(), "Name:\tinit\nVmRSS:\t   2048 kB\n", descriptors=4)
        self.assertEqual(samples["croccante_process_cpu_seconds_total"], 5.0)
        self.assertEqual(samples["croccante_process_resident_memory_bytes"], 2048 * 1024)
        self.assertEqual(samples["croccante_process_open_fds"], 4)

    def test_unparseable_cpu_counters_report_zero(self) -> None:
        samples = process_samples(stat_line(user="x"), "VmRSS:\t10 kB\n")
        self.assertEqual(samples["croccante_process_cpu_seconds_total"], 0.0)

    def test_short_stat_or_missing_clock_ticks_report_zero_cpu(self) -> None:
        self.assertEqual(process_samples("1 (init) S", "")["croccante_process_cpu_seconds_total"], 0.0)
        self.assertEqual(process_samples(stat_line(), "", ticks=0)["croccante_process_cpu_seconds_total"], 0.0)

    def test_malformed_or_absent_resident_memory_reports_zero(self) -> None:
        for status in ("VmRSS:\n", "VmRSS:\tmany kB\n", "Name:\tinit\n", ""):
            with self.subTest(status=status):
                samples = process_samples(stat_line(), status)
                self.assertEqual(samples["croccante_process_resident_memory_bytes"], 0)

    def test_unreadable_descriptor_table_reports_zero(self) -> None:
        samples = process_samples(stat_line(), "", descriptors=PermissionError("denied"))
        self.assertEqual(samples["croccante_process_open_fds"], 0)


class ParsingBoundaryTest(unittest.TestCase):
    def test_non_integer_counters_fall_back_to_their_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "counter"
            path.write_text("not-a-number\n", encoding="utf-8")
            self.assertEqual(relay_metrics._read_nonnegative_int(path, 7), 7)
            path.write_text("-5\n", encoding="utf-8")
            self.assertEqual(relay_metrics._read_nonnegative_int(path, 7), 0)
            self.assertEqual(relay_metrics._read_nonnegative_int(Path(directory) / "missing", 9), 9)

    def test_non_finite_samples_are_exposed_as_zero(self) -> None:
        self.assertEqual(relay_metrics._sample("croccante_example", float("nan")), "croccante_example 0")
        self.assertEqual(relay_metrics._sample("croccante_example", float("inf"), slot="1"), 'croccante_example{slot="1"} 0')

    def test_label_values_are_escaped(self) -> None:
        self.assertEqual(relay_metrics._escape_label('a"b\\c\nd'), 'a\\"b\\\\c\\nd')
        self.assertEqual(relay_metrics._labels({}), "")

    def test_unsafe_build_versions_are_not_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(relay_metrics, "STATE_DIR", Path(directory)):
            with patch.object(relay_metrics, "BUILD_VERSION", "bad version with spaces"):
                payload = relay_metrics.collect()
        self.assertIn('version="unknown"', payload)
        self.assertNotIn("bad version", payload)


if __name__ == "__main__":
    unittest.main()
