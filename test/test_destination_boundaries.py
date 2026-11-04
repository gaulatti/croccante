#!/usr/bin/env python3
"""Selection, secret-provider and supervisor-lifecycle boundaries."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import destination_runtime
import destination_store
from destination_runtime import DestinationRuntime
from destination_store import (
    AWSSecretsManagerProvider,
    DestinationError,
    FileSecretProvider,
    build_url,
    parse_selection,
    provider_from_environment,
    resolve_selection,
)

REFERENCE = {"id": "primary", "secretId": "secret-a", "versionId": "version-a"}
SECRET = {"scheme": "rtmp", "host": "ingest.example", "application": "live", "streamKey": "fictional"}


def reason_of(call, *args, **kwargs) -> str:
    try:
        call(*args, **kwargs)
    except DestinationError as error:
        return error.reason
    raise AssertionError("expected a DestinationError")


class SelectionParsingTest(unittest.TestCase):
    def test_every_malformed_selection_has_a_stable_reason(self) -> None:
        cases = {
            "not-an-object": ("invalid_selection", ["x"]),
            "extra-field": ("invalid_selection", {"version": "v1", "destinations": [REFERENCE], "extra": 1}),
            "version-type": ("invalid_version", {"version": 5, "destinations": [REFERENCE]}),
            "version-shape": ("invalid_version", {"version": "has space", "destinations": [REFERENCE]}),
            "destinations-type": ("invalid_destination_count", {"version": "v1", "destinations": "x"}),
            "destinations-empty": ("invalid_destination_count", {"version": "v1", "destinations": []}),
            "reference-type": ("invalid_reference", {"version": "v1", "destinations": ["x"]}),
            "id-type": ("invalid_destination_id", {"version": "v1", "destinations": [{**REFERENCE, "id": 5}]}),
            "id-shape": ("invalid_destination_id", {"version": "v1", "destinations": [{**REFERENCE, "id": "-bad"}]}),
            "secret-type": ("invalid_secret_reference", {"version": "v1", "destinations": [{**REFERENCE, "secretId": 5}]}),
            "version-id-type": ("invalid_secret_version", {"version": "v1", "destinations": [{**REFERENCE, "versionId": 5}]}),
            "version-id-shape": ("invalid_secret_version", {"version": "v1", "destinations": [{**REFERENCE, "versionId": "bad id"}]}),
        }
        for name, (reason, value) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(reason_of(parse_selection, value), reason)

    def test_expected_version_must_match_the_body(self) -> None:
        selection = {"version": "v1", "destinations": [REFERENCE]}
        self.assertEqual(parse_selection(selection, expected_version="v1").version, "v1")
        self.assertEqual(reason_of(parse_selection, selection, expected_version="v2"), "version_mismatch")

    def test_selection_hash_binds_the_exact_references(self) -> None:
        one = parse_selection({"version": "v1", "destinations": [REFERENCE]})
        other = parse_selection({"version": "v1", "destinations": [{**REFERENCE, "versionId": "version-b"}]})
        self.assertNotEqual(one.selection_hash, other.selection_hash)


class UrlConstructionTest(unittest.TestCase):
    def test_ip_literal_hosts_and_ports_form_valid_authorities(self) -> None:
        self.assertEqual(build_url({**SECRET, "host": "192.0.2.10", "port": 1935}), "rtmp://192.0.2.10:1935/live/fictional")
        self.assertEqual(build_url({**SECRET, "host": "2001:db8::1"}), "rtmp://[2001:db8::1]/live/fictional")
        self.assertEqual(build_url({**SECRET, "streamKey": "a%/?"}), "rtmp://ingest.example/live/a%25%2F%3F")

    def test_every_invalid_component_has_a_stable_reason(self) -> None:
        cases = {
            "missing-field": ("invalid_secret_payload", {k: v for k, v in SECRET.items() if k != "host"}),
            "scheme": ("unsupported_scheme", {**SECRET, "scheme": "http"}),
            "host-type": ("invalid_host", {**SECRET, "host": 5}),
            "host-shape": ("invalid_host", {**SECRET, "host": "bad host"}),
            "application-type": ("invalid_application", {**SECRET, "application": 5}),
            "application-shape": ("invalid_application", {**SECRET, "application": "-live"}),
            "application-traversal": ("invalid_application", {**SECRET, "application": "live/../other"}),
            "key-type": ("invalid_stream_key", {**SECRET, "streamKey": 5}),
            "key-empty": ("invalid_stream_key", {**SECRET, "streamKey": ""}),
            "key-too-long": ("invalid_stream_key", {**SECRET, "streamKey": "k" * 513}),
            "key-control": ("invalid_stream_key", {**SECRET, "streamKey": "a\x01b"}),
            "port-bool": ("invalid_port", {**SECRET, "port": True}),
            "port-type": ("invalid_port", {**SECRET, "port": "1935"}),
            "port-low": ("invalid_port", {**SECRET, "port": 0}),
            "port-high": ("invalid_port", {**SECRET, "port": 65536}),
        }
        for name, (reason, secret) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(reason_of(build_url, secret), reason)


class FailingProvider:
    def get(self, secret_id: str, version_id: str) -> dict[str, object]:
        raise RuntimeError("provider exploded with private detail")


class ResolutionTest(unittest.TestCase):
    def test_unexpected_provider_failures_are_reported_as_unavailable(self) -> None:
        selection = parse_selection({"version": "v1", "destinations": [REFERENCE]})
        error = None
        try:
            resolve_selection(selection, FailingProvider())
        except DestinationError as caught:
            error = caught
        assert error is not None
        self.assertEqual(error.reason, "secret_unavailable")
        self.assertNotIn("private detail", str(error))


class FakeSecretsClient:
    def __init__(self, response: object = None, failure: Exception | None = None) -> None:
        self.response = response
        self.failure = failure
        self.requests: list[dict[str, str]] = []

    def get_secret_value(self, **request: str) -> object:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return self.response


def fake_boto3(client: FakeSecretsClient) -> types.ModuleType:
    module = types.ModuleType("boto3")
    module.client = lambda service, region_name=None: client  # type: ignore[attr-defined]
    return module


class AwsProviderTest(unittest.TestCase):
    def provider(self, client: FakeSecretsClient) -> AWSSecretsManagerProvider:
        with patch.dict(sys.modules, {"boto3": fake_boto3(client)}):
            return AWSSecretsManagerProvider()

    def test_requests_the_exact_secret_version(self) -> None:
        client = FakeSecretsClient({"VersionId": "version-a", "SecretString": json.dumps(SECRET)})
        self.assertEqual(self.provider(client).get("secret-a", "version-a"), SECRET)
        self.assertEqual(client.requests, [{"SecretId": "secret-a", "VersionId": "version-a"}])

    def test_missing_packaging_is_reported_without_a_fallback(self) -> None:
        with patch.dict(sys.modules, {"boto3": None}):
            self.assertEqual(reason_of(AWSSecretsManagerProvider), "provider_unavailable")

    def test_every_provider_failure_has_a_stable_reason(self) -> None:
        cases = {
            "client-failure": ("secret_unavailable", FakeSecretsClient(failure=RuntimeError("denied"))),
            "other-version": ("secret_version_mismatch", FakeSecretsClient({"VersionId": "other", "SecretString": "{}"})),
            "binary-secret": ("secret_version_mismatch", FakeSecretsClient({"VersionId": "version-a", "SecretBinary": b"x"})),
            "invalid-json": ("invalid_secret_payload", FakeSecretsClient({"VersionId": "version-a", "SecretString": "{"})),
            "not-an-object": ("invalid_secret_payload", FakeSecretsClient({"VersionId": "version-a", "SecretString": "[]"})),
        }
        for name, (reason, client) in cases.items():
            with self.subTest(case=name):
                self.assertEqual(reason_of(self.provider(client).get, "secret-a", "version-a"), reason)


class FileProviderTest(unittest.TestCase):
    def test_rejects_unreadable_or_malformed_fixture_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(reason_of(FileSecretProvider, root / "missing.json"), "provider_unavailable")
            (root / "broken.json").write_text("{", encoding="utf-8")
            self.assertEqual(reason_of(FileSecretProvider, root / "broken.json"), "provider_unavailable")
            (root / "list.json").write_text("[]", encoding="utf-8")
            self.assertEqual(reason_of(FileSecretProvider, root / "list.json"), "provider_unavailable")

    def test_lookup_requires_an_exact_secret_and_version_object(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.json"
            path.write_text(json.dumps({"known": {"v1": SECRET, "scalar": "x"}, "flat": "x"}), encoding="utf-8")
            provider = FileSecretProvider(path)
            self.assertEqual(provider.get("known", "v1"), SECRET)
            for secret_id, version in (("unknown", "v1"), ("known", "v2"), ("known", "scalar"), ("flat", "v1")):
                with self.subTest(secret=secret_id, version=version):
                    self.assertEqual(reason_of(provider.get, secret_id, version), "secret_unavailable")


class ProviderSelectionTest(unittest.TestCase):
    def environment(self, **values: str):
        return patch.dict(destination_store.os.environ, values, clear=True)

    def test_aws_is_the_default_provider(self) -> None:
        client = FakeSecretsClient()
        with self.environment(), patch.dict(sys.modules, {"boto3": fake_boto3(client)}):
            self.assertIsInstance(provider_from_environment(), AWSSecretsManagerProvider)
        with self.environment(DESTINATION_SECRET_PROVIDER="aws", AWS_REGION="us-east-1"), patch.dict(sys.modules, {"boto3": fake_boto3(client)}):
            self.assertIsInstance(provider_from_environment(), AWSSecretsManagerProvider)

    def test_file_provider_requires_a_local_environment_and_a_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets.json"
            path.write_text("{}", encoding="utf-8")
            with self.environment(DESTINATION_SECRET_PROVIDER="file", CROCCANTE_ENVIRONMENT="test", DESTINATION_FAKE_SECRETS_FILE=str(path)):
                self.assertIsInstance(provider_from_environment(), FileSecretProvider)
            with self.environment(DESTINATION_SECRET_PROVIDER="file", CROCCANTE_ENVIRONMENT="local"):
                self.assertEqual(reason_of(provider_from_environment), "provider_unavailable")
            with self.environment(DESTINATION_SECRET_PROVIDER="file"):
                self.assertEqual(reason_of(provider_from_environment), "file_provider_forbidden")

    def test_unknown_providers_are_refused(self) -> None:
        with self.environment(DESTINATION_SECRET_PROVIDER="vault"):
            self.assertEqual(reason_of(provider_from_environment), "provider_unavailable")


def plan(*urls: str):
    selection = parse_selection({
        "version": "v1",
        "destinations": [{"id": f"d{index}", "secretId": f"s{index}", "versionId": "v1"} for index, _ in enumerate(urls)],
    })
    return destination_store.ResolvedSelection(
        selection,
        tuple(destination_store.ResolvedDestination(f"d{index}", url) for index, url in enumerate(urls)),
    )


class FakeProcess:
    def __init__(self, *, exits_on_terminate: bool = True, polled: int | None = None) -> None:
        self.pid = 4242
        self.polled = polled
        self.exits_on_terminate = exits_on_terminate
        self.terminated = False
        self.killed = False
        self.waits: list[float] = []

    def poll(self) -> int | None:
        return self.polled

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float) -> int:
        self.waits.append(timeout)
        if not self.exits_on_terminate and not self.killed:
            raise subprocess.TimeoutExpired("supervisor", timeout)
        return 0


class RuntimeLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def supervisor(self, body: str) -> str:
        path = self.root / "supervisor.sh"
        path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
        path.chmod(0o700)
        return str(path)

    def test_activation_publishes_private_state_and_deactivation_removes_it(self) -> None:
        runtime = DestinationRuntime(self.root, self.supervisor("trap 'exit 0' TERM INT\nwhile true; do sleep 1; done"))
        self.assertEqual(runtime.public_ids(), [])
        with patch.object(destination_runtime.time, "sleep"):
            runtime.activate(plan("rtmp://one.example/live/key-one", "rtmp://two.example/live/key-two"))
        try:
            self.assertEqual(runtime.public_ids(), ["d0", "d1"])
            self.assertEqual((self.root / "dest.count").read_text().strip(), "2")
            self.assertEqual((self.root / "dest-1.url").read_text().strip(), "rtmp://one.example/live/key-one")
            self.assertEqual((self.root / "dest-2.id").read_text().strip(), "d1")
            self.assertEqual((self.root / "dest-1.url").stat().st_mode & 0o777, 0o600)
        finally:
            runtime.deactivate()
        self.assertEqual(runtime.public_ids(), [])
        self.assertEqual((self.root / "dest.count").read_text().strip(), "0")
        self.assertEqual(list(self.root.glob("dest-*")), [])

    def test_unreapable_supervisors_are_killed_after_the_grace_period(self) -> None:
        stubborn, finished = FakeProcess(exits_on_terminate=False), FakeProcess(polled=0)
        runtime = DestinationRuntime(self.root)
        runtime.processes = [stubborn, finished]  # type: ignore[list-item]
        runtime.deactivate()
        self.assertTrue(stubborn.terminated)
        self.assertTrue(stubborn.killed)
        self.assertFalse(finished.terminated)
        self.assertFalse(finished.killed)
        self.assertEqual(runtime.processes, [])

    def test_exec_failures_roll_back_every_started_supervisor(self) -> None:
        started = FakeProcess()
        runtime = DestinationRuntime(self.root, "/missing/relay-dest.sh")
        outcomes = [started, FileNotFoundError("exec failed")]

        def launch(command: list[str]) -> object:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(destination_runtime.subprocess, "Popen", launch):
            self.assertEqual(reason_of(runtime.activate, plan("rtmp://a.example/l/1", "rtmp://b.example/l/2")), "supervisor_start_failed")
        self.assertTrue(started.terminated)
        self.assertIsNone(runtime.active)
        self.assertEqual(runtime.processes, [])
        self.assertEqual(list(self.root.glob("dest-*")), [])
        self.assertEqual((self.root / "dest.count").read_text().strip(), "0")


if __name__ == "__main__":
    unittest.main()
