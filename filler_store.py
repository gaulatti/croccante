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
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
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


BITRATE = re.compile(r"^[1-9][0-9]*(?:k|M)$")
PROFILE_NAMES = {
    "baseline": {"Baseline", "Constrained Baseline"},
    "main": {"Main"},
    "high": {"High"},
    "high444": {"High 4:4:4 Predictive"},
}
LEVELS = {10, 11, 12, 13, 20, 21, 22, 30, 31, 32, 40, 41, 42, 50, 51, 52, 60, 61, 62}
PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow", "placebo"}
EXTRA_FIELDS = {
    "contractVersion", "videoMinrate", "videoMaxrate", "videoBufsize", "h264Profile",
    "pixelFormat", "h264Level", "maxBFrames", "minKeyint", "sceneThreshold", "encoderPreset",
}
BASE_TYPES = {
    "width": int, "height": int, "fps": int, "videoBitrate": str,
    "audioRate": int, "audioChannels": int, "audioBitrate": str,
    "gop": int, "loopSeconds": int,
}


def validate_shape(raw: dict[str, Any]) -> None:
    if set(raw) != set(BASE_TYPES) | EXTRA_FIELDS:
        raise ValueError("invalid-profile")
    if any(type(raw[key]) is not kind for key, kind in BASE_TYPES.items()):
        raise ValueError("invalid-profile")


def bitrate(value: object) -> int:
    if not isinstance(value, str) or not BITRATE.fullmatch(value):
        raise ValueError("invalid-profile")
    return int(value[:-1]) * (1000 if value[-1] == "k" else 1_000_000)


def integer(value: object, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("invalid-profile")
    return value


@dataclass(frozen=True)
class ExactEncoding:
    minimum: str | None
    maximum: str
    buffer: str
    profile: str
    pixel_format: str
    level: int
    b_frames: int
    min_keyint: int
    scene_threshold: int
    preset: str

    @classmethod
    def parse(cls, raw: dict[str, Any], base: dict[str, object]) -> "ExactEncoding":
        validate_shape(raw)
        if type(raw["contractVersion"]) is not int or raw["contractVersion"] != 2:
            raise ValueError("invalid-profile")
        minimum = raw["videoMinrate"]
        target = bitrate(base["videoBitrate"])
        if minimum is not None and bitrate(minimum) > target:
            raise ValueError("invalid-profile")
        if bitrate(raw["videoMaxrate"]) < target:
            raise ValueError("invalid-profile")
        bitrate(raw["videoBufsize"])
        profile = raw["h264Profile"]
        pixel_format = raw["pixelFormat"]
        if not isinstance(profile, str) or profile not in PROFILE_NAMES:
            raise ValueError("invalid-profile")
        expected_format = "yuv444p" if profile == "high444" else "yuv420p"
        if pixel_format != expected_format:
            raise ValueError("invalid-profile")
        level = integer(raw["h264Level"], 10, 62)
        if level not in LEVELS:
            raise ValueError("invalid-profile")
        b_frames = integer(raw["maxBFrames"], 0, 16)
        if profile == "baseline" and b_frames != 0:
            raise ValueError("invalid-profile")
        min_keyint = integer(raw["minKeyint"], 1, int(base["gop"]))
        scene = integer(raw["sceneThreshold"], 0, 100)
        preset = raw["encoderPreset"]
        if not isinstance(preset, str) or preset not in PRESETS:
            raise ValueError("invalid-profile")
        return cls(minimum, raw["videoMaxrate"], raw["videoBufsize"], profile,
                   pixel_format, level, b_frames, min_keyint, scene, preset)

    def public(self) -> dict[str, object]:
        return {
            "contractVersion": 2, "videoMinrate": self.minimum,
            "videoMaxrate": self.maximum, "videoBufsize": self.buffer,
            "h264Profile": self.profile, "pixelFormat": self.pixel_format,
            "h264Level": self.level, "maxBFrames": self.b_frames,
            "minKeyint": self.min_keyint, "sceneThreshold": self.scene_threshold,
            "encoderPreset": self.preset,
        }

    def arguments(self, target: str, gop: int) -> list[str]:
        args = [
            "-c:v", "libx264", "-preset", self.preset, "-tune", "zerolatency",
            "-profile:v", self.profile, "-pix_fmt", self.pixel_format,
            "-level:v", f"{self.level // 10}.{self.level % 10}",
            "-bf", str(self.b_frames), "-b:v", target,
        ]
        if self.minimum is not None:
            args += ["-minrate", self.minimum]
        return args + [
            "-maxrate", self.maximum, "-bufsize", self.buffer,
            "-g", str(gop), "-keyint_min", str(self.min_keyint),
            "-sc_threshold", str(self.scene_threshold),
        ]

    def validate_video(self, video: dict[str, Any]) -> None:
        # Rate control/GOP options are encoder configuration, not an assertion
        # that every frame has the requested average bitrate. Check the observable
        # stream properties separately; the complete configuration is acknowledged.
        observed_profile = video.get("profile")
        if not isinstance(observed_profile, str) or observed_profile not in PROFILE_NAMES[self.profile]:
            raise ValueError("profile-mismatch")
        if type(video.get("level")) is not int or video["level"] != self.level:
            raise ValueError("profile-mismatch")
        reordered = video.get("has_b_frames")
        if type(reordered) is not int or not 0 <= reordered <= self.b_frames:
            raise ValueError("profile-mismatch")


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
    encoding: ExactEncoding | None = None

    @classmethod
    def parse(cls, raw: object, *, exact: bool = False) -> "Profile":
        if not isinstance(raw, dict):
            raise PreparationError("invalid-profile")
        if exact:
            try:
                validate_shape(raw)
            except ValueError as exc:
                raise PreparationError("invalid-profile") from exc
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
        if exact:
            try:
                profile = replace(profile, encoding=ExactEncoding.parse(raw, profile.public()))
            except ValueError as exc:
                raise PreparationError("invalid-profile") from exc
        return profile

    def public(self) -> dict[str, object]:
        result = {
            "width": self.width, "height": self.height, "fps": self.fps,
            "videoBitrate": self.video_bitrate, "audioRate": self.audio_rate,
            "audioChannels": self.audio_channels, "audioBitrate": self.audio_bitrate,
            "gop": self.gop, "loopSeconds": self.loop_seconds,
        }
        if self.encoding is not None:
            result.update(self.encoding.public())
        return result


class FillerStore:
    def __init__(self, root: Path, program_id: str, *, exact_profiles: bool = False) -> None:
        self.root = root
        self.program_id = program_id
        self.exact_profiles = exact_profiles
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

    def ready_for_start(self, version: str) -> bool:
        manifest = self.manifest(version)
        if manifest is None:
            return False
        if not self.exact_profiles:
            return True
        try:
            profile = Profile.parse(manifest.get("profile"), exact=True)
            self._validate_output(self._probe(self.version_dir(version) / "filler.flv"), profile)
        except PreparationError:
            return False
        return True

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
        profile = Profile.parse(request.get("profile"), exact=self.exact_profiles)
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
        command += ["-vf", f"scale={profile.width}:{profile.height}:force_original_aspect_ratio=decrease,pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2,fps={profile.fps}"]
        video_arguments = [
            "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "main", "-pix_fmt", "yuv420p",
            "-b:v", profile.video_bitrate, "-minrate", profile.video_bitrate, "-maxrate", profile.video_bitrate,
            "-bufsize", profile.video_bitrate, "-g", str(profile.gop), "-keyint_min", str(profile.gop), "-sc_threshold", "0",
        ]
        if profile.encoding is not None:
            video_arguments = profile.encoding.arguments(profile.video_bitrate, profile.gop)
        command += video_arguments + [
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
        pixel_format = profile.encoding.pixel_format if profile.encoding is not None else "yuv420p"
        if output_fps != profile.fps or video.get("pix_fmt") != pixel_format:
            raise PreparationError("profile-mismatch")
        if int(audio.get("sample_rate", 0)) != profile.audio_rate or int(audio.get("channels", 0)) != profile.audio_channels:
            raise PreparationError("profile-mismatch")
        if profile.encoding is not None:
            try:
                profile.encoding.validate_video(video)
            except ValueError as exc:
                raise PreparationError("profile-mismatch") from exc
