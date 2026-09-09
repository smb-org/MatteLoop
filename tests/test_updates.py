from __future__ import annotations

from collections.abc import Callable, Mapping

from matteloop.jobs.models.download import DownloadHttpError
from matteloop.updates import (
    GITHUB_LATEST_RELEASE_URL,
    GitHubUpdateReader,
    UpdateOutcome,
)


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


def _reader(body: bytes) -> tuple[GitHubUpdateReader, _Transport, _Response]:
    response = _Response(body)
    transport = _Transport(response)
    return GitHubUpdateReader(transport), transport, response


def test_newer_release_is_reported_as_an_update() -> None:
    reader, transport, response = _reader(b'{"tag_name":"v0.4.0"}')

    result = reader.check()

    assert result.outcome is UpdateOutcome.UPDATE
    assert result.version == "0.4.0"
    assert transport.calls == [
        (
            GITHUB_LATEST_RELEASE_URL,
            {"Accept": "application/vnd.github+json", "User-Agent": "MatteLoop"},
        )
    ]
    assert response.closed


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
