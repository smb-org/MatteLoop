from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from threading import Event

import pytest

from matteloop.jobs.models.download import DownloadHttpError
from matteloop.updates import (
    GITHUB_LATEST_RELEASE_URL,
    GitHubUpdateReader,
    UpdateDownloadCancelled,
    UpdateOutcome,
    download_update,
    update_feed_url,
    update_package_url,
)
from tests.update_fakes import FakeResponse, FakeTransport


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body
        self._read = False
        self.headers: Mapping[str, str] = {}
        self.read_sizes: list[int] = []
        self.closed = False

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        if self._read:
            return b""
        self._read = True
        return self._body

    def close(self) -> None:
        self.closed = True


class _Transport:
    def __init__(
        self,
        response: _Response | None = None,
        *,
        failure: BaseException | None = None,
    ) -> None:
        self.response = response
        self.failure = failure
        self.calls: list[tuple[str, Mapping[str, str] | None]] = []

    def open(
        self,
        url: str,
        _cancelled: Callable[[], bool],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> _Response:
        self.calls.append((url, headers))
        if self.failure is not None:
            raise self.failure
        assert self.response is not None
        return self.response


def _package_fixture(
    package: bytes = b"nupkg-content",
    *,
    version: str = "0.4.0",
    package_response: FakeResponse | BaseException | None = None,
) -> tuple[FakeTransport, str]:
    filename = "io.github.smb-org.matteloop-0.4.0-osx-arm64-full.nupkg"
    feed = json.dumps(
        {
            "Assets": [
                {
                    "Version": version,
                    "Type": "Delta",
                    "FileName": "old.delta.nupkg",
                    "SHA256": "wrong",
                    "Size": 1,
                },
                {
                    "Version": version,
                    "Type": "Full",
                    "FileName": filename,
                    "SHA256": hashlib.sha256(package).hexdigest().upper(),
                    "Size": len(package),
                },
            ]
        }
    ).encode()
    return (
        FakeTransport(
            {
                update_feed_url(version, platform="darwin"): FakeResponse(feed),
                update_package_url(version, filename): package_response
                or FakeResponse(package),
            }
        ),
        filename,
    )


def _reader(body: bytes) -> tuple[GitHubUpdateReader, _Transport, _Response]:
    response = _Response(body)
    transport = _Transport(response)
    return GitHubUpdateReader(transport), transport, response


def test_newer_release_is_reported_as_an_update(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    reader, transport, response = _reader(
        json.dumps(
            {
                "tag_name": "v0.4.0",
                "assets": [
                    {
                        "name": "MatteLoop-win-x64-full.nupkg",
                        "size": 123,
                    },
                    {
                        "name": "MatteLoop-osx-arm64-full.nupkg",
                        "size": 398_458_880,
                    },
                ],
            }
        ).encode()
    )

    result = reader.check()

    assert result.outcome is UpdateOutcome.UPDATE
    assert result.version == "0.4.0"
    assert result.size == 398_458_880
    assert transport.calls == [
        (
            GITHUB_LATEST_RELEASE_URL,
            {"Accept": "application/vnd.github+json", "User-Agent": "MatteLoop"},
        )
    ]
    assert response.closed


def test_newer_release_without_a_platform_package_has_no_download_size(
    monkeypatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    reader, _, _ = _reader(
        b'{"tag_name":"v0.4.0","assets":[{"name":"MatteLoop-source.zip","size":123}]}'
    )

    assert reader.check().size is None


def test_equal_release_is_reported_as_no_update() -> None:
    reader, _, _ = _reader(b'{"tag_name":"v0.3.0"}')

    assert reader.check().outcome is UpdateOutcome.NONE


def test_older_release_is_reported_as_no_update() -> None:
    reader, _, _ = _reader(b'{"tag_name":"v0.2.9"}')

    assert reader.check().outcome is UpdateOutcome.NONE


def test_non_semver_release_tag_is_reported_as_no_update() -> None:
    reader, _, _ = _reader(b'{"tag_name":"release-0.4.0"}')

    assert reader.check().outcome is UpdateOutcome.NONE


def test_non_json_release_body_is_reported_as_failed() -> None:
    reader, _, _ = _reader(b"not json")

    assert reader.check().outcome is UpdateOutcome.FAILED


def test_release_body_over_the_cap_is_reported_as_failed() -> None:
    reader, _, response = _reader(
        b'{"tag_name":"v0.4.0","body":"' + b"x" * (1024 * 1024) + b'"}'
    )

    assert reader.check().outcome is UpdateOutcome.FAILED
    assert response.read_sizes == [1024 * 1024 + 1]


def test_http_error_is_reported_as_failed() -> None:
    reader = GitHubUpdateReader(
        _Transport(failure=DownloadHttpError(503))
    )

    assert reader.check().outcome is UpdateOutcome.FAILED


def test_repository_environment_overrides_the_notice_feed(monkeypatch) -> None:
    monkeypatch.setenv("MATTELOOP_UPDATE_REPO", "qualification/MatteLoop")
    reader, transport, _ = _reader(b'{"tag_name":"v0.4.0"}')

    assert reader.check().outcome is UpdateOutcome.UPDATE
    assert transport.calls[0][0] == (
        "https://api.github.com/repos/qualification/MatteLoop/releases/latest"
    )


def test_a_failing_close_keeps_the_outcome_read_from_the_body() -> None:
    class _UnclosableResponse(_Response):
        def close(self) -> None:
            raise OSError("connection reset while closing")

    response = _UnclosableResponse(b'{"tag_name":"v0.4.0"}')
    reader = GitHubUpdateReader(_Transport(response))

    result = reader.check()

    assert result.outcome is UpdateOutcome.UPDATE
    assert result.version == "0.4.0"


def test_update_download_selects_the_full_asset_and_verifies_case_insensitive_sha256(
    tmp_path: Path,
) -> None:
    transport, filename = _package_fixture()
    progress: list[tuple[int, int]] = []

    target = download_update(
        transport,
        "0.4.0",
        tmp_path,
        lambda completed, total: progress.append((completed, total)),
        lambda: False,
        platform="darwin",
    )

    assert target == tmp_path / filename
    assert target.read_bytes() == b"nupkg-content"
    assert not (tmp_path / f"{filename}.part").exists()
    assert progress[-1] == (len(b"nupkg-content"), len(b"nupkg-content"))


def test_update_download_checksum_mismatch_removes_the_part_file(
    tmp_path: Path,
) -> None:
    transport, filename = _package_fixture(
        package_response=FakeResponse(b"different-content")
    )
    feed_url = update_feed_url("0.4.0", platform="darwin")
    package_url = update_package_url("0.4.0", filename)
    body = json.loads(transport.responses[feed_url].body)
    body["Assets"][1]["SHA256"] = hashlib.sha256(b"nupkg-content").hexdigest()
    transport.responses[feed_url] = FakeResponse(json.dumps(body).encode())

    with pytest.raises(ValueError, match="SHA256"):
        download_update(
            transport,
            "0.4.0",
            tmp_path,
            lambda _completed, _total: None,
            lambda: False,
            platform="darwin",
        )

    assert not (tmp_path / filename).exists()
    assert not (tmp_path / f"{filename}.part").exists()
    assert transport.calls == [feed_url, package_url]


def test_update_download_cancellation_removes_the_part_file(tmp_path: Path) -> None:
    cancellation = Event()
    transport, filename = _package_fixture(
        package_response=FakeResponse(b"nupkg-content", on_read=cancellation.set)
    )

    with pytest.raises(UpdateDownloadCancelled):
        download_update(
            transport,
            "0.4.0",
            tmp_path,
            lambda _completed, _total: None,
            cancellation.is_set,
            platform="darwin",
        )

    assert not (tmp_path / filename).exists()
    assert not (tmp_path / f"{filename}.part").exists()


def test_update_download_transport_error_removes_the_part_file(tmp_path: Path) -> None:
    transport, filename = _package_fixture(package_response=OSError("offline"))

    with pytest.raises(OSError, match="offline"):
        download_update(
            transport,
            "0.4.0",
            tmp_path,
            lambda _completed, _total: None,
            lambda: False,
            platform="darwin",
        )

    assert not (tmp_path / filename).exists()
    assert not list(tmp_path.glob("*.part"))


def test_channel_refuses_a_platform_with_no_published_channel() -> None:
    """A dev machine must not be offered another platform's packages."""
    import pytest

    from matteloop.updates import UnsupportedUpdatePlatform, update_channel

    assert update_channel("darwin") == "osx-arm64"
    assert update_channel("win32") == "win-x64"
    with pytest.raises(UnsupportedUpdatePlatform):
        update_channel("linux")
