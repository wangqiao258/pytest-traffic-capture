"""针对兼容性辅助函数的专项单元测试。"""

from __future__ import annotations

import asyncio
import io
import os
import shlex
from types import SimpleNamespace

import pytest

from pytest_traffic_capture.adapters import (
    _aiohttp_not_sent,
    _aiohttp_payload,
    _BodyCollector,
    _make_reader_readuntil,
    _request_from_requests,
    _snapshot_file,
    _TeeReader,
)
from pytest_traffic_capture.exporters import build_curl
from pytest_traffic_capture.model import Exchange, RequestRecord
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


def test_aiohttp_quote_fields_false_keeps_raw_filename():
    aiohttp = pytest.importorskip("aiohttp")
    form = aiohttp.FormData(quote_fields=False)
    form.add_field(
        "file", b"abc", filename="文件.txt", content_type="text/plain"
    )

    payload = _aiohttp_payload(form)

    assert payload.parts[0].filename == "文件.txt"
    assert payload.parts[0].wire_filename == "文件.txt"


def test_aiohttp_connector_error_is_treated_as_not_sent():
    aiohttp = pytest.importorskip("aiohttp")
    from aiohttp.client_reqrep import ConnectionKey

    key = ConnectionKey("127.0.0.1", 1, False, False, None, None, None)
    exc = aiohttp.ClientConnectorError(key, OSError("refused"))

    assert _aiohttp_not_sent(exc) is True
    assert _aiohttp_not_sent(RuntimeError("boom")) is False


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


def test_curl_quotes_method_tokens_with_shell_metacharacters():
    exchange = Exchange(
        request=RequestRecord(method="GET|`id`", url="http://example.test/x")
    )

    curl = build_curl([exchange])
    command = next(line for line in curl.splitlines() if line.startswith("curl "))

    assert shlex.split(command) == [
        "curl",
        "-X",
        "GET|`ID`",
        "http://example.test/x",
    ]
    assert "-X 'GET|`ID`'" in curl


def test_curl_skips_invalid_method_and_warns():
    exchange = Exchange(
        request=RequestRecord(method="GET; id", url="http://example.test/x")
    )

    curl = build_curl([exchange])
    command = next(line for line in curl.splitlines() if line.startswith("curl "))

    assert "invalid HTTP method" in curl
    assert "-X" not in shlex.split(command)
    assert "GET; ID" not in shlex.split(command)


@pytest.mark.parametrize("method", ["GET\nID", "GET\n", "GET; id"])
def test_curl_comment_does_not_break_on_newline_method(method):
    exchange = Exchange(
        request=RequestRecord(method=method, url="http://example.test/x")
    )

    curl = build_curl([exchange])

    for line in curl.splitlines():
        if line.strip():
            assert line.startswith("#") or line.startswith("curl ")


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
