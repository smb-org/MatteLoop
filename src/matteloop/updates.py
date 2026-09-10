"""Read the latest public MatteLoop release from GitHub."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from enum import Enum

from matteloop import __version__
from matteloop.jobs.models.download import DownloadResponse, DownloadTransport

_LOGGER = logging.getLogger(__name__)
_MAX_RESPONSE_BYTES = 1024 * 1024
_VERSION_PATTERN = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
GITHUB_LATEST_RELEASE_URL = (
    "https://api.github.com/repos/smb-org/MatteLoop/releases/latest"
)
GITHUB_RELEASES_URL = "https://github.com/smb-org/MatteLoop/releases"
_REQUEST_HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "MatteLoop",
}


class UpdateOutcome(Enum):
    """The three outcomes of a release check."""

    UPDATE = "update"
    NONE = "none"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class UpdateResult:
    """A release-check outcome and its available version, when applicable."""

    outcome: UpdateOutcome
    version: str | None = None


class GitHubUpdateReader:
    """Read and compare the latest tagged GitHub release."""

    def __init__(self, transport: DownloadTransport) -> None:
        if not isinstance(transport, DownloadTransport):
            raise TypeError("transport must implement the bounded download protocol")
        self._transport = transport

    def check(self) -> UpdateResult:
        """Return whether GitHub advertises a newer compatible version."""
        response: DownloadResponse | None = None
        result = UpdateResult(UpdateOutcome.FAILED)
        try:
            response = self._transport.open(
                GITHUB_LATEST_RELEASE_URL,
                lambda: False,
                headers=_REQUEST_HEADERS,
            )
            payload = json.loads(_read_response(response))
            tag = payload.get("tag_name") if isinstance(payload, dict) else None
            release_version = _parse_tag(tag)
            if release_version is None:
                result = UpdateResult(UpdateOutcome.NONE)
            elif release_version > _current_version():
                result = UpdateResult(
                    UpdateOutcome.UPDATE, _version_text(release_version)
                )
            else:
                result = UpdateResult(UpdateOutcome.NONE)
        except Exception as error:
            _LOGGER.info("GitHub update check failed: %s", error)
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception as error:
                    _LOGGER.info("GitHub update response close failed: %s", error)
        return result


def _read_response(response: DownloadResponse) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while total <= _MAX_RESPONSE_BYTES:
        chunk = response.read(_MAX_RESPONSE_BYTES + 1 - total)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise ValueError("GitHub release response is too large")
    raise ValueError("GitHub release response is too large")


def _parse_tag(tag: object) -> tuple[int, int, int] | None:
    if not isinstance(tag, str):
        return None
    match = _VERSION_PATTERN.fullmatch(tag)
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _current_version() -> tuple[int, int, int]:
    match = _VERSION_PATTERN.fullmatch(f"v{__version__}")
    if match is None:
        raise ValueError("MatteLoop version is not semantic")
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _version_text(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)
