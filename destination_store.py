"""Bounded destination selection and AWS Secrets Manager resolution."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol
from urllib.parse import quote


MAX_DESTINATIONS = 20
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_ID = re.compile(r"^[A-Za-z0-9/_+=,.@:-]{1,512}$")
_HOST = re.compile(r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$")
_APP = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,127}$")
_SECRET_FIELDS = frozenset({"scheme", "host", "port", "application", "streamKey"})


class DestinationError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class DestinationReference:
    destination_id: str
    secret_id: str
    version_id: str


@dataclass(frozen=True)
class DestinationSelection:
    version: str
    references: tuple[DestinationReference, ...]
    selection_hash: str


@dataclass(frozen=True)
class ResolvedDestination:
    destination_id: str
    url: str


@dataclass(frozen=True)
class ResolvedSelection:
    selection: DestinationSelection
    destinations: tuple[ResolvedDestination, ...]


class SecretProvider(Protocol):
    def get(self, secret_id: str, version_id: str) -> Mapping[str, object]: ...


def canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def parse_selection(value: object, *, expected_version: str | None = None) -> DestinationSelection:
    if not isinstance(value, dict) or set(value) != {"version", "destinations"}:
        raise DestinationError("invalid_selection")
    version = value.get("version")
    destinations = value.get("destinations")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise DestinationError("invalid_version")
    if expected_version is not None and version != expected_version:
        raise DestinationError("version_mismatch")
    if not isinstance(destinations, list) or not 1 <= len(destinations) <= MAX_DESTINATIONS:
        raise DestinationError("invalid_destination_count")

    references: list[DestinationReference] = []
    seen: set[str] = set()
    for item in destinations:
        if not isinstance(item, dict) or set(item) != {"id", "secretId", "versionId"}:
            raise DestinationError("invalid_reference")
        destination_id = item.get("id")
        secret_id = item.get("secretId")
        version_id = item.get("versionId")
        if not isinstance(destination_id, str) or not _ID.fullmatch(destination_id):
            raise DestinationError("invalid_destination_id")
        if destination_id in seen:
            raise DestinationError("duplicate_destination")
        if not isinstance(secret_id, str) or not _SECRET_ID.fullmatch(secret_id):
            raise DestinationError("invalid_secret_reference")
        if not isinstance(version_id, str) or not _VERSION.fullmatch(version_id):
            raise DestinationError("invalid_secret_version")
        seen.add(destination_id)
        references.append(DestinationReference(destination_id, secret_id, version_id))

    digest_payload = [
        {"id": item.destination_id, "secretId": item.secret_id, "versionId": item.version_id}
        for item in references
    ]
    selection_hash = hashlib.sha256(canonical_json({"version": version, "destinations": digest_payload}).encode()).hexdigest()
    return DestinationSelection(version, tuple(references), selection_hash)


def _valid_host(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_HOST.fullmatch(host))


def build_url(secret: Mapping[str, object]) -> str:
    if set(secret) - _SECRET_FIELDS or not {"scheme", "host", "application", "streamKey"}.issubset(secret):
        raise DestinationError("invalid_secret_payload")
    scheme = secret.get("scheme")
    host = secret.get("host")
    application = secret.get("application")
    stream_key = secret.get("streamKey")
    port = secret.get("port")
    if scheme not in ("rtmp", "rtmps"):
        raise DestinationError("unsupported_scheme")
    if not isinstance(host, str) or not _valid_host(host):
        raise DestinationError("invalid_host")
    if not isinstance(application, str) or not _APP.fullmatch(application) or ".." in application.split("/"):
        raise DestinationError("invalid_application")
    if not isinstance(stream_key, str) or not 1 <= len(stream_key) <= 512 or any(char.isspace() or ord(char) < 32 for char in stream_key):
        raise DestinationError("invalid_stream_key")
    try:
        authority = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        authority = host
    if port is not None:
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise DestinationError("invalid_port")
        authority = f"{authority}:{port}"
    encoded_key = quote(stream_key, safe="-._~")
    return f"{scheme}://{authority}/{application}/{encoded_key}"


def resolve_selection(selection: DestinationSelection, provider: SecretProvider) -> ResolvedSelection:
    resolved: list[ResolvedDestination] = []
    for reference in selection.references:
        try:
            secret = provider.get(reference.secret_id, reference.version_id)
        except DestinationError:
            raise
        except Exception as exc:
            raise DestinationError("secret_unavailable") from exc
        resolved.append(ResolvedDestination(reference.destination_id, build_url(secret)))
    return ResolvedSelection(selection, tuple(resolved))


class AWSSecretsManagerProvider:
    def __init__(self) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover - image packaging guard
            raise DestinationError("provider_unavailable") from exc
        self.client = boto3.client("secretsmanager", region_name=os.environ.get("AWS_REGION"))

    def get(self, secret_id: str, version_id: str) -> Mapping[str, object]:
        try:
            response = self.client.get_secret_value(SecretId=secret_id, VersionId=version_id)
        except Exception as exc:
            raise DestinationError("secret_unavailable") from exc
        if response.get("VersionId") != version_id or not isinstance(response.get("SecretString"), str):
            raise DestinationError("secret_version_mismatch")
        try:
            value = json.loads(response["SecretString"])
        except json.JSONDecodeError as exc:
            raise DestinationError("invalid_secret_payload") from exc
        if not isinstance(value, dict):
            raise DestinationError("invalid_secret_payload")
        return value


class FileSecretProvider:
    """Explicit non-production provider for deterministic local relay tests."""

    def __init__(self, path: Path) -> None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DestinationError("provider_unavailable") from exc
        if not isinstance(value, dict):
            raise DestinationError("provider_unavailable")
        self.secrets = value

    def get(self, secret_id: str, version_id: str) -> Mapping[str, object]:
        versions = self.secrets.get(secret_id)
        value = versions.get(version_id) if isinstance(versions, dict) else None
        if not isinstance(value, dict):
            raise DestinationError("secret_unavailable")
        return value


def provider_from_environment() -> SecretProvider:
    provider = os.environ.get("DESTINATION_SECRET_PROVIDER", "aws")
    if provider == "aws":
        return AWSSecretsManagerProvider()
    if provider == "file":
        if os.environ.get("CROCCANTE_ENVIRONMENT") not in ("local", "test"):
            raise DestinationError("file_provider_forbidden")
        path = os.environ.get("DESTINATION_FAKE_SECRETS_FILE", "")
        if not path:
            raise DestinationError("provider_unavailable")
        return FileSecretProvider(Path(path))
    raise DestinationError("provider_unavailable")
