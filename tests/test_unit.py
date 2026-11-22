"""针对兼容性辅助函数的专项单元测试。"""

from __future__ import annotations

import asyncio
import io
import os
from types import SimpleNamespace

import pytest

from pytest_traffic_capture.adapters import (
    _aiohttp_body_from_kwargs,
    _aiohttp_not_sent,
    _aiohttp_payload,
    _BodyCollector,
    _httpx_not_sent,
    _make_reader_readuntil,
    _request_from_requests,
    _requests_not_sent,
    _snapshot_file,
    _tee_async_stream,
    _tee_sync_stream,
    _TeeReader,
)
from pytest_traffic_capture.exporters import (
    _request_cookies,
    _response_cookies,
)
from pytest_traffic_capture.recorder import (
    _TestSession,
    _atomic_write_text,
    _safe_hint,
    safe_nodeid,
)


def test_readuntil_does_not_forward_missing_max_size():
    calls = []

    async def original(reader, separator=b"\n"):
        calls.append((reader, separator))
        return b"line\n"

    wrapper = _make_reader_readuntil(original)
    reader = object()

    assert asyncio.run(wrapper(reader, b"\n")) == b"line\n"
    assert calls == [(reader, b"\n")]


def test_readuntil_forwards_max_size_when_given():
    calls = []

    async def original(reader, separator=b"\n", *, max_size=None):
        calls.append((reader, separator, max_size))
        return b"line\n"

    wrapper = _make_reader_readuntil(original)

    assert asyncio.run(wrapper("reader", b"\n", max_size=40)) == b"line\n"
    assert calls == [("reader", b"\n", 40)]


def test_tee_reader_records_stream_bytes():
    stream = io.BytesIO(b"abcdef")
    collector = _BodyCollector()
    reader = _TeeReader(stream, collector)

    assert reader.read(2) == b"ab"
    assert reader.read() == b"cdef"
    assert b"".join(collector.chunks) == b"abcdef"
    assert reader.read() == b""
    assert collector.eof is True


def test_aiohttp_connector_error_is_treated_as_not_sent():
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp.client_reqrep import ConnectionKey

    key = ConnectionKey("127.0.0.1", 1, False, False, None, None, None)
    exc = aiohttp.ClientConnectorError(key, OSError("refused"))

    assert _aiohttp_not_sent(exc) is True
    assert _aiohttp_not_sent(RuntimeError("boom")) is False


def test_requests_connect_timeout_is_treated_as_not_sent():
    requests = pytest.importorskip("requests")

    exc = requests.exceptions.ConnectTimeout("timed out")

    assert _requests_not_sent(exc) is True


def test_requests_protocol_error_is_not_treated_as_not_sent():
    requests = pytest.importorskip("requests")
    from urllib3.exceptions import ProtocolError

    exc = requests.exceptions.ConnectionError(ProtocolError("reset"))

    assert _requests_not_sent(exc) is False


def test_aiohttp_seekable_file_is_snapshotted_and_restored():
    handle = io.BytesIO(b"abc")
    payload = _aiohttp_payload(handle)

    assert payload.body == b"abc"
    assert payload.unreadable is False
    assert handle.tell() == 0


class _NonSeekable:
    name = "stream.bin"

    def __init__(self) -> None:
        self._data = b"abc"

    def read(self, size=-1):
        data = self._data if size < 0 else self._data[:size]
        self._data = self._data[len(data):]
        return data

    def fileno(self):
        raise OSError("no file descriptor")


def test_aiohttp_non_seekable_file_records_metadata():
    payload = _aiohttp_payload(_NonSeekable())

    assert payload.body is None
    assert payload.unreadable is True
    assert payload.source == "stream.bin"


def test_aiohttp_formdata_text_field_content_type():
    aiohttp = pytest.importorskip("aiohttp")

    form = aiohttp.FormData()
    form.add_field("meta", "中文")
    form.add_field("file", io.BytesIO(b"x"), filename="x.bin")
    payload = _aiohttp_payload(form)

    assert payload.parts is not None
    assert payload.parts[0].content_type == "text/plain; charset=utf-8"


def test_aiohttp_dict_bytes_becomes_multipart_file_field():
    payload = _aiohttp_payload({"raw": b"\xff\xfe"})

    assert payload.unreadable is False
    assert payload.parts is not None
    part = payload.parts[0]
    assert part.name == "raw"
    assert part.filename == "raw"
    assert part.value == b"\xff\xfe"
    assert part.content_type == "application/octet-stream"


def test_aiohttp_pair_list_is_urlencoded():
    payload = _aiohttp_payload([("a", "1"), ("a", "2")])

    assert payload.unreadable is False
    assert payload.body == b"a=1&a=2"


def test_requests_seekable_body_is_snapshotted_and_restored():
    handle = io.BytesIO(b"abc")
    prepared = SimpleNamespace(
        headers={},
        body=handle,
        method="POST",
        url="http://example.test/upload",
    )

    record = _request_from_requests(prepared)

    assert record.body == b"abc"
    assert record.unreadable is False
    assert handle.tell() == 0


def test_parameterized_nodeid_sidecar_is_cleaned(tmp_path):
    session = _TestSession("tests/test_param.py::test_case[alpha]", tmp_path)
    session.save_body("file.bin", b"secret")

    assert list((tmp_path / "bodies").iterdir())

    session.cleanup_previous()

    assert list((tmp_path / "bodies").iterdir()) == []


def test_snapshot_file_does_not_read_non_regular_stream():
    read_fd, write_fd = os.pipe()
    try:
        class _PipeStream:
            name = "pipe.bin"
            read_calls = 0

            def read(self, size=-1):
                type(self).read_calls += 1
                return b"payload"

            def tell(self):
                return 0

            def seek(self, position, whence=0):
                return 0

            def seekable(self):
                return True

            def fileno(self):
                return read_fd

        stream = _PipeStream()
        snapshot = _snapshot_file(stream)

        assert snapshot.data is None
        assert snapshot.unreadable is True
        assert stream.read_calls == 0
        assert snapshot.source == "pipe.bin"
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_snapshot_file_rejects_stream_that_reports_unseekable():
    class _Stream:
        def read(self):
            return b"payload"

        def tell(self):
            return 0

        def seek(self, position):
            return 0

        def seekable(self):
            return False

    snapshot = _snapshot_file(_Stream())

    assert snapshot.data is None
    assert snapshot.unreadable is True


class _UnknownSizeStream:
    """可回绕但没有 ``fileno`` 的二进制流，模拟拿不到文件大小的情况。"""

    mode = "rb"

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0
        self.bytes_read = 0

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._data) - self._position
        end = self._position + size
        data = self._data[self._position:end]
        self._position = end
        self.bytes_read += len(data)
        return data

    def tell(self):
        return self._position

    def seek(self, position, whence=0):
        self._position = position
        return position

    def seekable(self):
        return True

    def fileno(self):
        raise OSError("no file descriptor")


def test_snapshot_file_bounds_read_when_size_is_unknown():
    stream = _UnknownSizeStream(b"x" * 4096)

    snapshot = _snapshot_file(stream, limit=100)

    assert snapshot.truncated is True
    assert snapshot.data == b"x" * 100
    assert stream.bytes_read == 101
    assert stream.tell() == 0


def test_snapshot_file_keeps_body_below_limit_when_size_is_unknown():
    stream = _UnknownSizeStream(b"y" * 50)

    snapshot = _snapshot_file(stream, limit=100)

    assert snapshot.truncated is False
    assert snapshot.data == b"y" * 50
    assert stream.tell() == 0


class _TextStream:
    """文本模式的可回绕流，读取按字符计量。"""

    mode = "r"

    def __init__(self, data: str) -> None:
        self._data = data
        self._position = 0
        self.chars_read = 0

    def read(self, size=-1):
        end = len(self._data) if size < 0 else self._position + size
        data = self._data[self._position:end]
        self._position += len(data)
        self.chars_read += len(data)
        return data

    def tell(self):
        return self._position

    def seek(self, position, whence=0):
        self._position = position
        return position

    def seekable(self):
        return True

    def fileno(self):
        raise OSError("no file descriptor")


def test_snapshot_file_bounds_text_mode_read():
    stream = _TextStream("x" * 4096)

    snapshot = _snapshot_file(stream, limit=100)

    assert snapshot.truncated is True
    assert snapshot.data == b"x" * 100
    assert stream.chars_read == 101
    assert stream.tell() == 0


def test_request_cookies_are_parsed():
    cookies = _request_cookies([("Cookie", "a=1; b=2")])

    assert cookies == [
        {"name": "a", "value": "1"},
        {"name": "b", "value": "2"},
    ]


def test_response_cookies_keep_duplicate_headers():
    cookies = _response_cookies(
        [
            ("Set-Cookie", "a=1; Path=/"),
            ("Set-Cookie", "b=2; Path=/"),
        ]
    )

    assert [cookie["name"] for cookie in cookies] == ["a", "b"]


def test_safe_hint_strips_control_characters():
    assert _safe_hint("\x00evil\x1f") == "evil"


def test_safe_nodeid_uses_traffic_prefix():
    name = safe_nodeid("tests/test_api.py::test_login")

    assert name.startswith("traffic_")
    assert not name.startswith("test")


def test_atomic_write_replaces_target(tmp_path):
    path = tmp_path / "artifact.har"
    path.write_text("old", encoding="utf-8")

    _atomic_write_text(path, "new")

    assert path.read_text(encoding="utf-8") == "new"
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_preserves_target_on_failure(tmp_path, monkeypatch):
    path = tmp_path / "artifact.har"
    path.write_text("old", encoding="utf-8")

    def fail_replace(source, destination):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError):
        _atomic_write_text(path, "new")

    assert path.read_text(encoding="utf-8") == "old"
    assert list(tmp_path.glob("*.tmp")) == []


def test_sidecar_write_failure_does_not_raise(tmp_path, monkeypatch):
    session = _TestSession("tests/test_fail.py::test_fail", tmp_path)

    def fail(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(
        "pytest_traffic_capture.recorder._atomic_write_bytes", fail
    )

    with pytest.warns(RuntimeWarning):
        assert session.save_body("body.bin", b"payload") == ""


def test_package_version_matches_metadata():
    from importlib import metadata

    from pytest_traffic_capture import __version__

    try:
        installed = metadata.version("pytest-traffic-capture")
    except metadata.PackageNotFoundError:
        pytest.skip("package is not installed")
    assert __version__ == installed


def test_aiohttp_generated_payloads_respect_body_limit(monkeypatch):
    monkeypatch.setattr(
        "pytest_traffic_capture.adapters._body_limit", lambda: 8
    )

    for payload in (
        _aiohttp_payload("x" * 20),
        _aiohttp_payload(b"x" * 20),
        _aiohttp_payload({"blob": "x" * 20}),
        _aiohttp_payload([("blob", "x" * 20)]),
        _aiohttp_body_from_kwargs({"json": {"blob": "x" * 20}}),
    ):
        assert len(payload.body) == 8
        assert payload.truncated is True
        assert payload.size is not None and payload.size > 8


def test_aiohttp_formdata_fields_respect_body_limit(monkeypatch, tmp_path):
    aiohttp = pytest.importorskip("aiohttp")
    monkeypatch.setattr(
        "pytest_traffic_capture.adapters._body_limit", lambda: 8
    )

    path = tmp_path / "a.bin"
    path.write_bytes(b"abcdefghij")
    form = aiohttp.FormData()
    form.add_field("blob", "x" * 20)
    with open(path, "rb") as handle:
        form.add_field("file", handle, filename="a.bin")
        payload = _aiohttp_payload(form)

    assert payload.parts is not None
    blob, file_part = payload.parts
    assert blob.value == b"x" * 8
    assert blob.truncated is True
    assert blob.size == 20
    assert file_part.value == b"abcdefgh"
    assert file_part.truncated is True
    assert file_part.size == 10
    assert payload.truncated is True


def test_httpx_stream_consumed_is_treated_as_not_sent():
    httpx = pytest.importorskip("httpx")

    assert _httpx_not_sent(httpx.StreamConsumed()) is True
    assert _httpx_not_sent(RuntimeError("boom")) is False


def test_httpx_tee_stream_captures_first_pass_only():
    pytest.importorskip("httpx")

    class _Replay:
        def __iter__(self):
            yield b"ab"
            yield b"cd"

    collector = _BodyCollector()
    tee = _tee_sync_stream(_Replay(), collector)

    assert b"".join(tee) == b"abcd"
    assert b"".join(tee) == b"abcd"
    assert collector.eof is True
    assert b"".join(collector.chunks) == b"abcd"


def test_httpx_async_tee_stream_captures_first_pass_only():
    pytest.importorskip("httpx")

    class _Replay:
        async def __aiter__(self):
            for chunk in (b"ab", b"cd"):
                yield chunk

    collector = _BodyCollector()

    async def consume():
        tee = _tee_async_stream(_Replay(), collector)
        first = b"".join([chunk async for chunk in tee])
        second = b"".join([chunk async for chunk in tee])
        return first, second

    first, second = asyncio.run(consume())

    assert first == second == b"abcd"
    assert collector.eof is True
    assert b"".join(collector.chunks) == b"abcd"
