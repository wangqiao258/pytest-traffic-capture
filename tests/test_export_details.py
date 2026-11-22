"""针对响应编码与重定向的 HAR 保真度测试。"""

from __future__ import annotations

import base64

import pytest

from pytest_traffic_capture.exporters import build_har
from pytest_traffic_capture.model import Exchange, RequestRecord, ResponseRecord
from pytest_traffic_capture.multipart import Part

GIF_BYTES = b"GIF89a-this-is-ascii-not-really-a-gif"


def test_binary_response_is_base64_not_text(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import requests

        def test_binary():
            response = requests.get({url!r} + "/binary", timeout=5)
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    content = entry["response"]["content"]
    assert content["mimeType"] == "image/gif"
    assert content["encoding"] == "base64"
    assert base64.b64decode(content["text"]) == GIF_BYTES


def test_redirect_content_length_zero_is_not_unknown(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import requests

        def test_redirect():
            response = requests.get(
                {url!r} + "/redirect/302", timeout=5
            )
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    assert entries[0]["response"]["content"]["size"] == 0
    assert entries[0]["response"]["bodySize"] == 0


def test_har_preserves_content_type_parameters():
    request = RequestRecord(
        method="POST",
        url="http://example.test/",
        headers=[("Content-Type", "text/plain; charset=utf-8")],
        body="hello".encode("utf-8"),
    )
    response = ResponseRecord(
        status=200,
        headers=[("Content-Type", "text/html; charset=utf-8")],
        body=b"ok",
        mime_type="text/html; charset=utf-8",
    )

    har = build_har([Exchange(request=request, response=response)], max_inline_bytes=0)
    entry = har["log"]["entries"][0]

    assert entry["request"]["postData"]["mimeType"] == "text/plain; charset=utf-8"
    assert entry["response"]["content"]["mimeType"] == "text/html; charset=utf-8"


def test_multipart_metadata_and_raw_fields():
    request = RequestRecord(
        method="POST",
        url="http://example.test/upload",
        headers=[
            ("Content-Type", "multipart/form-data; boundary=b"),
            ("Content-Length", "123"),
        ],
        parts=[
            Part(
                name="jsonfield",
                value=b'{"a": 1}',
                content_type="application/json",
            ),
            Part(name="binfield", value=b"\xff\xfe\x00binary"),
            Part(
                name="note",
                value=b"hello",
                filename="note.txt",
                content_type="text/plain; charset=utf-8",
            ),
        ],
    )
    saved = {}

    def save_body(hint, data):
        name = "bodies/{}_{}".format(len(saved), hint)
        saved[name] = data
        return name

    har = build_har(
        [Exchange(request=request)], save_body=save_body, max_inline_bytes=0
    )
    post_data = har["log"]["entries"][0]["request"]["postData"]
    params = {item["name"]: item for item in post_data["params"]}
    assert "_file" not in post_data

    assert params["jsonfield"]["value"] == '{"a": 1}'
    assert params["jsonfield"]["contentType"] == "application/json"
    assert "_file" not in params["jsonfield"]
    assert params["binfield"]["value"] == "\ufffd\ufffd\x00binary"
    assert params["binfield"]["_file"]
    assert params["note"]["fileName"] == "note.txt"
    assert params["note"]["contentType"] == "text/plain; charset=utf-8"
    assert params["note"]["_file"]


def test_max_inline_zero_keeps_text_inline_but_sidecars_binary():
    saved = {}

    def save_body(hint, data):
        name = "bodies/{}_{}".format(len(saved), hint)
        saved[name] = data
        return name

    text_request = RequestRecord(
        method="POST",
        url="http://example.test/text",
        headers=[("Content-Type", "text/plain; charset=utf-8")],
        body=b"hello",
    )
    binary_request = RequestRecord(
        method="POST",
        url="http://example.test/binary",
        headers=[("Content-Type", "application/octet-stream")],
        body=b"\x00\x01",
    )

    har = build_har(
        [Exchange(request=text_request), Exchange(request=binary_request)],
        save_body=save_body,
        max_inline_bytes=0,
    )
    entries = har["log"]["entries"]

    assert entries[0]["request"]["postData"]["text"] == "hello"
    assert list(entries[1]["request"]["postData"]) == ["mimeType", "_file"]
    assert list(saved.values()) == [b"\x00\x01"]


def test_unsafe_multipart_field_name_stays_in_params():
    body = (
        b"--b\r\n"
        b'Content-Disposition: form-data; name="a=b"\r\n'
        b"\r\n"
        b"value\r\n"
        b"--b--\r\n"
    )
    request = RequestRecord(
        method="POST",
        url="http://example.test/multipart",
        headers=[
            ("Content-Type", "multipart/form-data; boundary=b"),
            ("Content-Length", str(len(body))),
        ],
        body=body,
    )

    har = build_har([Exchange(request=request)])

    post_data = har["log"]["entries"][0]["request"]["postData"]
    assert post_data["mimeType"] == "multipart/form-data; boundary=b"
    assert [(item["name"], item["value"]) for item in post_data["params"]] == [
        ("a=b", "value")
    ]
    assert "_file" not in post_data


def test_oversized_multipart_text_field_uses_sidecar():
    saved = {}

    def save_body(hint, data):
        name = "bodies/{}_{}".format(len(saved), hint)
        saved[name] = data
        return name

    request = RequestRecord(
        method="POST",
        url="http://example.test/upload",
        headers=[("Content-Type", "multipart/form-data; boundary=b")],
        parts=[
            Part(
                name="blob",
                value=b"x" * 100,
                content_type="text/plain; charset=utf-8",
            )
        ],
    )

    har = build_har(
        [Exchange(request=request)], save_body=save_body, max_inline_bytes=10
    )
    param = har["log"]["entries"][0]["request"]["postData"]["params"][0]

    assert param["name"] == "blob"
    assert param["value"] == ""
    assert param["_file"]
    assert saved[param["_file"]] == b"x" * 100
    assert "_truncated" not in param


def test_gzip_response_records_wire_size(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import requests

        def test_gzip():
            response = requests.get(
                {url!r} + "/gzip?size=1000", timeout=5
            )
            assert len(response.content) == 1000
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    headers = {
        header["name"].lower(): header["value"]
        for header in entry["response"]["headers"]
    }
    wire_size = int(headers["content-length"])
    content = entry["response"]["content"]
    assert content["size"] == 1000
    assert entry["response"]["bodySize"] == wire_size
    assert content["compression"] == 1000 - wire_size


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br"])
def test_compressed_response_compression_is_bytes_saved(encoding):
    request = RequestRecord(method="GET", url="http://example.test/gzip")
    response = ResponseRecord(
        status=200,
        headers=[
            ("Content-Encoding", encoding),
            ("Content-Length", "300"),
            ("Content-Type", "text/plain"),
        ],
        body=b"x" * 1000,
        mime_type="text/plain",
    )

    entry = build_har([Exchange(request=request, response=response)])["log"][
        "entries"
    ][0]
    content = entry["response"]["content"]

    assert content["size"] == 1000
    assert entry["response"]["bodySize"] == 300
    assert content["compression"] == 700


def test_chunked_response_body_size_is_payload_length():
    request = RequestRecord(method="GET", url="http://example.test/chunked")
    response = ResponseRecord(
        status=200,
        headers=[
            ("Transfer-Encoding", "chunked"),
            ("Content-Type", "text/plain"),
        ],
        body=b"x" * 70000,
        mime_type="text/plain",
    )

    entry = build_har([Exchange(request=request, response=response)])["log"][
        "entries"
    ][0]
    content = entry["response"]["content"]

    assert content["size"] == 70000
    assert entry["response"]["bodySize"] == 70000
    assert "compression" not in content


def test_head_gzip_response_body_size_is_zero():
    request = RequestRecord(method="HEAD", url="http://example.test/gzip")
    response = ResponseRecord(
        status=200,
        headers=[
            ("Content-Encoding", "gzip"),
            ("Content-Length", "250"),
            ("Content-Type", "text/plain"),
        ],
        body=b"",
        mime_type="text/plain",
    )

    entry = build_har([Exchange(request=request, response=response)])["log"][
        "entries"
    ][0]

    assert entry["response"]["bodySize"] == 0
    assert "compression" not in entry["response"]["content"]
