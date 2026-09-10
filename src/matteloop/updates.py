"""Read the latest public MatteLoop release from GitHub."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from functools import total_ordering
from pathlib import Path

from matteloop import __version__
from matteloop.jobs.models.download import (
    CancellationCheck,
    DownloadResponse,
    DownloadTransport,
)

_LOGGER = logging.getLogger(__name__)
_MAX_RESPONSE_BYTES = 1024 * 1024
_VERSION_PATTERN = re.compile(
    r"^v(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$"
)
_PRERELEASE_IDENTIFIER_PATTERN = re.compile(r"^[0-9A-Za-z-]+$")
GITHUB_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/smb-org/MatteLoop/releases/latest"
)
GITHUB_RELEASES_URL = "https://github.com/smb-org/MatteLoop/releases"
_DEFAULT_UPDATE_REPOSITORY = "smb-org/MatteLoop"
_UPDATE_REPOSITORY_ENV = "MATTELOOP_UPDATE_REPO"
_REQUEST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "MatteLoop",
}
_DOWNLOAD_CHUNK_SIZE = 256 * 1024
_OSX_CHANNEL = "osx-arm64"
_WINDOWS_CHANNEL = "win-x64"


class UpdateDownloadCancelled(Exception):
    """The user stopped an in-progress update download."""


class UnsupportedUpdatePlatform(Exception):
    """No update channel is published for the platform in use."""


def update_channel(platform: str) -> str:
    """Return the Velopack feed channel for a supported runtime platform."""
    if platform == "darwin":
        return _OSX_CHANNEL
    if platform == "win32":
        return _WINDOWS_CHANNEL
    raise UnsupportedUpdatePlatform(
        f"MatteLoop publishes no update channel for {platform}"
    )


def update_feed_url(version: str, *, platform: str) -> str:
    """Return the tagged Velopack feed URL for an available release."""
    tag = f"v{version}"
    return (
        f"{update_repository_url()}/releases/download/{tag}/"
        f"releases.{update_channel(platform)}.json"
    )


def update_package_url(version: str, filename: str) -> str:
    """Return the tagged Velopack package URL for an available release."""
    return f"{update_repository_url()}/releases/download/v{version}/{filename}"


def update_repository() -> str:
    """Return the GitHub ``owner/repository`` used by both update readers."""
    return os.environ.get(_UPDATE_REPOSITORY_ENV, _DEFAULT_UPDATE_REPOSITORY)


def update_repository_url() -> str:
    """Return the GitHub repository URL for the Velopack source."""
    return f"https://github.com/{update_repository()}"


def latest_release_url() -> str:
    """Return the API URL for the configured repository's latest release."""
    return f"https://api.github.com/repos/{update_repository()}/releases/latest"


def releases_api_url() -> str:
    """Return the API URL for all releases in the configured repository."""
    return f"https://api.github.com/repos/{update_repository()}/releases"


def releases_url() -> str:
    """Return the browser URL for the configured repository's releases."""
    return f"{update_repository_url()}/releases"


class UpdateOutcome(Enum):
    """The three outcomes of a release check."""

    UPDATE = "update"
    NONE = "none"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """A release-check outcome with available version and package size."""

    outcome: UpdateOutcome
    version: str | None = None
    size: int | None = None


@total_ordering
@dataclass(frozen=True, slots=True)
class _SemanticVersion:
    """The semver fields needed to compare release tags."""

    core: tuple[int, int, int]
    prerelease: tuple[str, ...] = ()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, _SemanticVersion):
            return NotImplemented
        if self.core != other.core:
            return self.core < other.core
        if not self.prerelease:
            return False
        if not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_numeric = left.isdigit()
            right_numeric = right.isdigit()
            if left_numeric and right_numeric:
                return int(left) < int(right)
            if left_numeric != right_numeric:
                return left_numeric
            return left < right
        return len(self.prerelease) < len(other.prerelease)


def download_update(
    transport: DownloadTransport,
    version: str,
    destination: Path,
    progress: Callable[[int, int], None],
    cancelled: CancellationCheck,
    *,
    platform: str,
) -> Path:
    """Fetch, verify, and atomically publish one Velopack full package."""
    feed_response: DownloadResponse | None = None
    try:
        feed_response = transport.open(
            update_feed_url(version, platform=platform), cancelled
        )
        _raise_if_cancelled(cancelled)
        feed = json.loads(_read_response(feed_response))
    finally:
        if feed_response is not None:
            feed_response.close()

    asset = _full_asset(feed, version)
    filename = asset["FileName"]
    checksum = asset["SHA256"]
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise ValueError("Velopack feed contains an invalid package filename")
    if not isinstance(checksum, str):
        raise ValueError("Velopack feed contains no SHA256 checksum")
    total = asset.get("Size")
    if type(total) is not int or total < 0:
        total = 0
    return _download_package(
        transport,
        version,
        destination,
        filename,
        checksum,
        total,
        progress,
        cancelled,
    )


def _download_package(
    transport: DownloadTransport,
    version: str,
    destination: Path,
    filename: str,
    checksum: str,
    total: int,
    progress: Callable[[int, int], None],
    cancelled: CancellationCheck,
) -> Path:
    target = destination / filename
    part = destination / f"{filename}.part"
    destination.mkdir(parents=True, exist_ok=True)
    part.unlink(missing_ok=True)
    response: DownloadResponse | None = None
    try:
        response = transport.open(update_package_url(version, filename), cancelled)
        _raise_if_cancelled(cancelled)
        digest = hashlib.sha256()
        completed = 0
        progress(0, total)
        with part.open("wb") as output:
            while True:
                _raise_if_cancelled(cancelled)
                chunk = response.read(_DOWNLOAD_CHUNK_SIZE)
                if type(chunk) is not bytes:
                    raise OSError("update transport returned a non-bytes chunk")
                if not chunk:
                    break
                completed += len(chunk)
                output.write(chunk)
                digest.update(chunk)
                progress(completed, total)
            _raise_if_cancelled(cancelled)
            output.flush()
            os.fsync(output.fileno())
        _raise_if_cancelled(cancelled)
        if digest.hexdigest().casefold() != checksum.casefold():
            raise ValueError("Velopack package SHA256 does not match its feed")
        response.close()
        response = None
        _raise_if_cancelled(cancelled)
        os.replace(part, target)
        _fsync_directory(destination)
        return target
    except BaseException:
        part.unlink(missing_ok=True)
        raise
    finally:
        if response is not None:
            response.close()


def _full_asset(feed: object, version: str) -> dict[str, object]:
    if not isinstance(feed, dict) or not isinstance(feed.get("Assets"), list):
        raise ValueError("Velopack feed contains no assets")
    for candidate in feed["Assets"]:
        if not isinstance(candidate, dict):
            continue
        if candidate.get("Type") == "Full" and candidate.get("Version") == version:
            return candidate
    raise ValueError(f"Velopack feed contains no full package for {version}")


def _read_response(
    response: DownloadResponse, cancelled: CancellationCheck | None = None
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= _MAX_RESPONSE_BYTES:
        chunk = response.read(_MAX_RESPONSE_BYTES + 1 - total)
        if type(chunk) is not bytes:
            raise OSError("update transport returned a non-bytes chunk")
        if cancelled is not None:
            _raise_if_cancelled(cancelled)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise ValueError("Velopack feed is too large")
    raise ValueError("Velopack feed is too large")


def _raise_if_cancelled(cancelled: CancellationCheck) -> None:
    if cancelled():
        raise UpdateDownloadCancelled()


def _fsync_directory(directory: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class GitHubUpdateReader:
    """Read and compare the latest tagged GitHub release."""

    def __init__(self, transport: DownloadTransport) -> None:
        if not isinstance(transport, DownloadTransport):
            raise TypeError("transport must implement the bounded download protocol")
        self._transport = transport

    def check(
        self,
        channel: str = "stable",
        current_version: str | None = None,
        cancelled: CancellationCheck | None = None,
    ) -> UpdateResult:
        """Return whether GitHub advertises a newer compatible version."""
        cancellation = cancelled or (lambda: False)
        response: DownloadResponse | None = None
        result = UpdateResult(UpdateOutcome.FAILED)
        try:
            response = self._transport.open(
                _release_api_url(channel),
                cancellation,
                headers=_REQUEST_HEADERS,
            )
            payload = json.loads(_read_response(response, cancellation))
            selected = _select_release(payload, channel)
            if selected is None:
                result = UpdateResult(UpdateOutcome.NONE)
            else:
                release_version, release = selected
                if release_version <= _current_version(current_version):
                    result = UpdateResult(UpdateOutcome.NONE)
                else:
                    result = UpdateResult(
                        UpdateOutcome.UPDATE,
                        _version_text(release_version),
                        _full_package_size(release, platform=sys.platform),
                    )
        except Exception as error:
            _LOGGER.info("GitHub update check failed: %s", error)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception as error:
                    _LOGGER.info("GitHub update response close failed: %s", error)
        return result


def _full_package_size(payload: object, *, platform: str) -> int | None:
    """Return the GitHub asset size for the current platform's full package."""
    try:
        channel = update_channel(platform)
    except UnsupportedUpdatePlatform:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("assets"), list):
        return None
    suffix = f"-{channel}-full.nupkg"
    for asset in payload["assets"]:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            continue
        if not asset["name"].endswith(suffix):
            continue
        size = asset.get("size")
        return size if type(size) is int and size >= 0 else None
    return None


def _release_api_url(channel: str) -> str:
    if channel == "stable":
        return latest_release_url()
    if channel == "beta":
        return releases_api_url()
    raise ValueError("unsupported update channel")


def _select_release(
    payload: object, channel: str
) -> tuple[_SemanticVersion, dict[str, object]] | None:
    if channel == "stable":
        if not isinstance(payload, dict):
            return None
        if payload.get("draft") is True or payload.get("prerelease") is True:
            return None
        version = _parse_tag(payload.get("tag_name"))
        return (version, payload) if version is not None else None
    if channel != "beta" or not isinstance(payload, list):
        return None
    newest: tuple[_SemanticVersion, dict[str, object]] | None = None
    for candidate in payload:
        if not isinstance(candidate, dict) or candidate.get("draft") is True:
            continue
        version = _parse_tag(candidate.get("tag_name"))
        if version is not None and (newest is None or version > newest[0]):
            newest = (version, candidate)
    return newest


def _parse_tag(tag: object) -> _SemanticVersion | None:
    if not isinstance(tag, str):
        return None
    match = _VERSION_PATTERN.fullmatch(tag)
    if match is None:
        return None
    prerelease_text = match.group(4)
    prerelease = () if prerelease_text is None else tuple(prerelease_text.split("."))
    if any(not _valid_prerelease_identifier(identifier) for identifier in prerelease):
        return None
    return _SemanticVersion(
        (int(match.group(1)), int(match.group(2)), int(match.group(3))),
        prerelease,
    )


def _valid_prerelease_identifier(identifier: str) -> bool:
    if _PRERELEASE_IDENTIFIER_PATTERN.fullmatch(identifier) is None:
        return False
    return not (identifier.isdigit() and len(identifier) > 1 and identifier[0] == "0")


def _current_version(version: str | None = None) -> _SemanticVersion:
    current = __version__ if version is None else version
    tag = current if current.startswith("v") else f"v{current}"
    parsed = _parse_tag(tag)
    if parsed is None:
        raise ValueError("MatteLoop version is not semantic")
    return parsed


def _version_text(version: _SemanticVersion) -> str:
    text = ".".join(str(part) for part in version.core)
    return text if not version.prerelease else f"{text}-{'.'.join(version.prerelease)}"
