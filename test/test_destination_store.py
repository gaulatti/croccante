#!/usr/bin/env python3

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from destination_store import (
    DestinationError,
    FileSecretProvider,
    build_url,
    parse_selection,
    provider_from_environment,
    resolve_selection,
)
from destination_runtime import DestinationRuntime


class DictProvider:
    def __init__(self, values: dict[tuple[str, str], dict[str, object]]) -> None:
        self.values = values

    def get(self, secret_id: str, version_id: str) -> dict[str, object]:
        try:
            return self.values[(secret_id, version_id)]
        except KeyError as exc:
            raise DestinationError("secret_unavailable") from exc


def selection(*destinations: dict[str, str]) -> dict[str, object]:
    return {"version": "broadcast-v7", "destinations": list(destinations)}


class DestinationStoreTest(unittest.TestCase):
    def test_resolves_exact_versioned_references_to_bounded_urls(self) -> None:
        parsed = parse_selection(selection(
            {"id": "youtube", "secretId": "youtube-secret", "versionId": "version-0001"},
            {"id": "facebook", "secretId": "facebook-secret", "versionId": "version-0002"},
        ))
        provider = DictProvider({
            ("youtube-secret", "version-0001"): {
                "scheme": "rtmp", "host": "a.rtmp.example", "application": "live2", "streamKey": "fake-key-a",
            },
            ("facebook-secret", "version-0002"): {
                "scheme": "rtmps", "host": "live.example", "port": 443, "application": "rtmp", "streamKey": "fake-key-b",
            },
        })
        resolved = resolve_selection(parsed, provider)
        self.assertEqual([item.destination_id for item in resolved.destinations], ["youtube", "facebook"])
        self.assertEqual(resolved.destinations[0].url, "rtmp://a.rtmp.example/live2/fake-key-a")
        self.assertEqual(resolved.destinations[1].url, "rtmps://live.example:443/rtmp/fake-key-b")
        self.assertEqual(len(parsed.selection_hash), 64)

    def test_rejects_unbounded_duplicate_or_malformed_selection(self) -> None:
        duplicate = selection(
            {"id": "same", "secretId": "a", "versionId": "version-a"},
            {"id": "same", "secretId": "b", "versionId": "version-b"},
        )
        with self.assertRaisesRegex(DestinationError, "duplicate_destination"):
            parse_selection(duplicate)
        with self.assertRaisesRegex(DestinationError, "invalid_destination_count"):
            parse_selection(selection(*[
                {"id": f"d{index}", "secretId": f"s{index}", "versionId": f"version-{index}"}
                for index in range(21)
            ]))
        with self.assertRaisesRegex(DestinationError, "invalid_reference"):
            parse_selection(selection({"id": "a", "secretId": "s", "versionId": "v", "url": "rtmp://forbidden"}))
        with self.assertRaisesRegex(DestinationError, "invalid_secret_reference"):
            parse_selection(selection({"id": "a", "secretId": "secret with spaces", "versionId": "v"}))

    def test_rejects_unsupported_or_overbroad_secret_payloads(self) -> None:
        with self.assertRaisesRegex(DestinationError, "unsupported_scheme"):
            build_url({"scheme": "https", "host": "example.test", "application": "live", "streamKey": "key"})
        with self.assertRaisesRegex(DestinationError, "invalid_secret_payload"):
            build_url({
                "scheme": "rtmp", "host": "example.test", "application": "live", "streamKey": "key",
                "publishUrl": "rtmp://must-not-be-accepted",
            })
        with self.assertRaisesRegex(DestinationError, "invalid_stream_key"):
            build_url({"scheme": "rtmp", "host": "example.test", "application": "live", "streamKey": "key with spaces"})

    def test_resolution_is_all_or_nothing(self) -> None:
        parsed = parse_selection(selection(
            {"id": "a", "secretId": "available", "versionId": "version-a"},
            {"id": "b", "secretId": "missing", "versionId": "version-b"},
        ))
        provider = DictProvider({
            ("available", "version-a"): {
                "scheme": "rtmp", "host": "example.test", "application": "live", "streamKey": "key",
            }
        })
        with self.assertRaisesRegex(DestinationError, "secret_unavailable"):
            resolve_selection(parsed, provider)

    def test_file_provider_is_explicitly_non_production(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.json"
            path.write_text(json.dumps({"secret": {"version": {"scheme": "rtmp"}}}))
            provider = FileSecretProvider(path)
            self.assertEqual(provider.get("secret", "version"), {"scheme": "rtmp"})
            with patch.dict(os.environ, {
                "DESTINATION_SECRET_PROVIDER": "file",
                "DESTINATION_FAKE_SECRETS_FILE": str(path),
                "CROCCANTE_ENVIRONMENT": "production",
            }, clear=False):
                with self.assertRaisesRegex(DestinationError, "file_provider_forbidden"):
                    provider_from_environment()

    def test_worker_start_is_atomic_and_rejects_duplicate_resolved_urls(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            supervisor = root / "supervisor.sh"
            supervisor.write_text(
                "#!/bin/sh\n"
                "mkdir \"$0.lock\" 2>/dev/null || exit 1\n"
                "trap 'exit 0' TERM INT\nwhile true; do sleep 1; done\n",
                encoding="utf-8",
            )
            supervisor.chmod(0o700)
            runtime = DestinationRuntime(root, str(supervisor))
            parsed = parse_selection({
                "version": "atomic-v1",
                "destinations": [
                    {"id": "one", "secretId": "one", "versionId": "v1"},
                    {"id": "two", "secretId": "two", "versionId": "v1"},
                ],
            })
            provider = DictProvider({
                ("one", "v1"): {"scheme": "rtmp", "host": "one.example", "application": "live", "streamKey": "one"},
                ("two", "v1"): {"scheme": "rtmp", "host": "two.example", "application": "live", "streamKey": "two"},
            })
            with self.assertRaisesRegex(DestinationError, "supervisor_start_failed"):
                runtime.activate(resolve_selection(parsed, provider))
            self.assertIsNone(runtime.active)
            self.assertEqual(runtime.processes, [])
            self.assertEqual((root / "dest.count").read_text().strip(), "0")
            self.assertEqual(list(root.glob("dest-*.url")), [])

            duplicate_provider = DictProvider({
                ("one", "v1"): {"scheme": "rtmp", "host": "same.example", "application": "live", "streamKey": "same"},
                ("two", "v1"): {"scheme": "rtmp", "host": "same.example", "application": "live", "streamKey": "same"},
            })
            with self.assertRaisesRegex(DestinationError, "duplicate_resolved_destination"):
                runtime.activate(resolve_selection(parsed, duplicate_provider))


if __name__ == "__main__":
    unittest.main()
