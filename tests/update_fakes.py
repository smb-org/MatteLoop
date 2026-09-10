"""Fake transport/response pair shared by the update download tests.

Used by tests/test_updates.py and tests/ui/test_update_controller.py, both of
which exercise download_update()/UpdateController against the bounded
DownloadTransport protocol rather than the network.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping


class FakeResponse:
    def __init__(
        self, body: bytes, *, on_read: Callable[[], None] | None = None
    ) -> None:
        self.body = body
        self.on_read = on_read
        self.read_once = False
        self.headers: Mapping[str, str] = {}
        self.closed = False

    def read(self, _size: int) -> bytes:
        if self.read_once:
            return b""
        self.read_once = True
        if self.on_read is not None:
            self.on_read()
        return self.body

    def close(self) -> None:
        self.closed = True


class FakeTransport:
    def __init__(self, responses: Mapping[str, FakeResponse | BaseException]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    def open(
        self,
        url: str,
        _cancelled: Callable[[], bool],
        *,
        headers: Mapping[str, str] | None = None,
    ) -> FakeResponse:
        del headers
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, BaseException):
            raise response
        return response
