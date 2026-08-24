#!/usr/bin/env python3
"""Durable, immutable Croccante filler preparation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
BITRATE = re.compile(r"^[1-9][0-9]*(?:k|M)$")
MAX_DOWNLOAD_BYTES = 512 * 1024 * 1024


class PreparationError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class Profile:
    width: int
    height: int
    fps: int
    video_bitrate: str
    audio_rate: int
    audio_channels: int
    audio_bitrate: str
    gop: int
    loop_seconds: int

    @classmethod
    def parse(cls, raw: object) -> "Profile":
        if not isinstance(raw, dict):
            raise PreparationError("invalid-profile")
        try:
            profile = cls(
                width=int(raw["width"]), height=int(raw["height"]), fps=int(raw["fps"]),
                video_bitrate=str(raw["videoBitrate"]), audio_rate=int(raw["audioRate"]),
                audio_channels=int(raw["audioChannels"]), audio_bitrate=str(raw["audioBitrate"]),
                gop=int(raw["gop"]), loop_seconds=int(raw.get("loopSeconds", 10)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise PreparationError("invalid-profile") from exc
        if not (16 <= profile.width <= 7680 and 16 <= profile.height <= 4320):
            raise PreparationError("invalid-profile")
        if profile.width % 2 or profile.height % 2:
            raise PreparationError("invalid-profile")
        if not (1 <= profile.fps <= 120 and 1 <= profile.gop <= 600):
            raise PreparationError("invalid-profile")
        if not (8000 <= profile.audio_rate <= 192000 and profile.audio_channels in (1, 2)):
            raise PreparationError("invalid-profile")
        if not (1 <= profile.loop_seconds <= 300):
            raise PreparationError("invalid-profile")
        if not BITRATE.fullmatch(profile.video_bitrate) or not BITRATE.fullmatch(profile.audio_bitrate):
            raise PreparationError("invalid-profile")
        return profile

    def public(self) -> dict[str, object]:
        return {
            "width": self.width, "height": self.height, "fps": self.fps,
            "videoBitrate": self.video_bitrate, "audioRate": self.audio_rate,
            "audioChannels": self.audio_channels, "audioBitrate": self.audio_bitrate,
            "gop": self.gop, "loopSeconds": self.loop_seconds,
        }


class FillerStore:
    def __init__(self, root: Path, program_id: str) -> None:
        self.root = root
        self.program_id = program_id
        self.program_root = root / hashlib.sha256(program_id.encode()).hexdigest()
        self.program_root.mkdir(parents=True, exist_ok=True)

    def version_dir(self, version: str) -> Path:
        if not IDENTIFIER.fullmatch(version):
            raise PreparationError("invalid-version")
        return self.program_root / version

    def manifest(self, version: str) -> dict[str, Any] | None:
        path = self.version_dir(version) / "manifest.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return None
        asset = path.parent / "filler.flv"
        if not asset.is_file() or sha256_file(asset) != value.get("artifactSha256"):
            return None
        return value

    def public_state(self, version: str) -> dict[str, object]:
        manifest = self.manifest(version)
        if manifest is None:
            return {"version": version, "status": "unprepared", "ready": False}
        return {key: manifest[key] for key in (
            "version", "status", "ready", "sourceId", "sourceSha256",
            "artifactSha256", "profile", "preparedAt",
        )}

    def prepare(self, version: str, request: dict[str, Any], prepared_at: str) -> dict[str, object]:
        target = self.version_dir(version)
        source = request.get("source")
        if not isinstance(source, dict):
            raise PreparationError("invalid-source")
        source_id = str(source.get("id", ""))
        source_sha = str(source.get("sha256", "")).lower()
        download_url = str(source.get("downloadUrl", ""))
        if not IDENTIFIER.fullmatch(source_id) or not re.fullmatch(r"[0-9a-f]{64}", source_sha):
            raise PreparationError("invalid-source")
        if not download_url.startswith(("https://", "http://")):
            raise PreparationError("invalid-download")
        profile = Profile.parse(request.get("profile"))
        semantic_request = {
            "commandId": request.get("commandId"),
            "source": {"id": source_id, "sha256": source_sha},
            "profile": profile.public(),
        }
        request_hash = hashlib.sha256(canonical_json(semantic_request).encode()).hexdigest()
        existing = self.manifest(version)
        if existing is not None:
            if existing.get("requestHash") != request_hash:
                raise PreparationError("version-conflict")
            return self.public_state(version)

        staging = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=self.program_root))
        try:
            source_path = staging / "source"
            self._download(download_url, source_path)
            if sha256_file(source_path) != source_sha:
                raise PreparationError("checksum-mismatch")
            probe = self._probe(source_path)
            self._transcode(source_path, staging / "filler.flv", profile, probe)
            output_probe = self._probe(staging / "filler.flv")
            self._validate_output(output_probe, profile)
            manifest = {
                "version": version, "status": "ready", "ready": True,
                "sourceId": source_id, "sourceSha256": source_sha,
                "artifactSha256": sha256_file(staging / "filler.flv"),
                "profile": profile.public(), "preparedAt": prepared_at,
                "requestHash": request_hash,
            }
            (staging / "manifest.json").write_text(canonical_json(manifest) + "\n", encoding="utf-8")
            quarantine: Path | None = None
            if target.exists():
                quarantine = self.program_root / f".{version}.invalid.{os.getpid()}"
                os.rename(target, quarantine)
            try:
                os.rename(staging, target)
            except Exception:
                if quarantine is not None and not target.exists():
                    os.rename(quarantine, target)
                raise
            if quarantine is not None:
                shutil.rmtree(quarantine)
            return self.public_state(version)
        except FileExistsError as exc:
            existing = self.manifest(version)
            if existing and existing.get("requestHash") == request_hash:
                return self.public_state(version)
            raise PreparationError("version-conflict") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def cleanup(self, active_version: str | None, retain: int = 3) -> None:
        versions = []
        for child in self.program_root.iterdir():
            if child.is_dir() and not child.name.startswith(".") and self.manifest(child.name):
                versions.append(child)
        versions.sort(key=lambda path: path.stat().st_mtime, reverse=True)
        keep = {path.name for path in versions[:retain]}
        if active_version:
            keep.add(active_version)
        for path in versions:
            if path.name not in keep:
                shutil.rmtree(path)

    @staticmethod
    def _download(url: str, destination: Path) -> None:
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "croccante-filler/1"})
            with urllib.request.urlopen(request, timeout=30) as response, destination.open("wb") as output:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise PreparationError("download-too-large")
                    output.write(chunk)
        except PreparationError:
            raise
        except Exception as exc:
            close = getattr(exc, "close", None)
            if callable(close):
                close()
            raise PreparationError("download-failed") from exc

    @staticmethod
    def _probe(path: Path) -> dict[str, Any]:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                check=True, capture_output=True, text=True, timeout=30,
            )
            return json.loads(result.stdout)
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            raise PreparationError("probe-failed") from exc

    @staticmethod
    def _transcode(source: Path, output: Path, profile: Profile, probe: dict[str, Any]) -> None:
        streams = probe.get("streams", [])
        has_video = any(item.get("codec_type") == "video" for item in streams)
        has_audio = any(item.get("codec_type") == "audio" for item in streams)
        if not has_video:
            raise PreparationError("missing-video")
        video = next(item for item in streams if item.get("codec_type") == "video")
        still = video.get("codec_name") in {"png", "mjpeg", "jpeg2000", "webp", "bmp"}
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        command += ["-loop", "1", "-i", str(source)] if still else ["-stream_loop", "-1", "-i", str(source)]
        if not has_audio:
            layout = "mono" if profile.audio_channels == 1 else "stereo"
            command += ["-f", "lavfi", "-i", f"anullsrc=sample_rate={profile.audio_rate}:channel_layout={layout}"]
        command += ["-t", str(profile.loop_seconds), "-map", "0:v:0", "-map", "0:a:0" if has_audio else "1:a:0"]
        command += [
            "-vf", f"scale={profile.width}:{profile.height}:force_original_aspect_ratio=decrease,pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2,fps={profile.fps}",
            "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main", "-pix_fmt", "yuv420p",
            "-b:v", profile.video_bitrate, "-minrate", profile.video_bitrate, "-maxrate", profile.video_bitrate,
            "-bufsize", profile.video_bitrate, "-g", str(profile.gop), "-keyint_min", str(profile.gop), "-sc_threshold", "0",
            "-c:a", "aac", "-b:a", profile.audio_bitrate, "-ar", str(profile.audio_rate), "-ac", str(profile.audio_channels),
            "-shortest", "-f", "flv", str(output),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=max(60, profile.loop_seconds * 6))
        except (subprocess.SubprocessError, OSError) as exc:
            raise PreparationError("transcode-failed") from exc

    @staticmethod
    def _validate_output(probe: dict[str, Any], profile: Profile) -> None:
        streams = probe.get("streams", [])
        video = next((item for item in streams if item.get("codec_type") == "video"), None)
        audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
        if not video or not audio or video.get("codec_name") != "h264" or audio.get("codec_name") != "aac":
            raise PreparationError("profile-mismatch")
        if int(video.get("width", 0)) != profile.width or int(video.get("height", 0)) != profile.height:
            raise PreparationError("profile-mismatch")
        try:
            output_fps = Fraction(video.get("avg_frame_rate", "0/1"))
        except (ValueError, ZeroDivisionError):
            output_fps = Fraction(0)
        if output_fps != profile.fps or video.get("pix_fmt") != "yuv420p":
            raise PreparationError("profile-mismatch")
        if int(audio.get("sample_rate", 0)) != profile.audio_rate or int(audio.get("channels", 0)) != profile.audio_channels:
            raise PreparationError("profile-mismatch")
