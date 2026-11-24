"""针对基于 requests 的捕获路径的回归测试。"""

from __future__ import annotations

import base64
import hashlib


def test_requests_redirect_chain_absolute_url(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import requests

        def test_chain():
            response = requests.get({url!r} + "/chain", timeout=5)
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    assert [entry["response"]["status"] for entry in entries] == [302, 301, 200]
    assert entries[0]["response"]["redirectURL"] == http_server + "/chain2"
    assert entries[1]["response"]["redirectURL"] == http_server + "/echo"


def test_requests_307_preserves_body(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import requests

        def test_307():
            response = requests.post(
                {url!r} + "/redirect/307", data=b"abc", timeout=5
            )
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    assert [entry["response"]["status"] for entry in entries] == [307, 200]
    assert entries[1]["request"]["method"] == "POST"
    assert entries[1]["request"]["bodySize"] == 3
    assert entries[1]["request"]["postData"]["text"] == "abc"


def test_requests_307_file_like_upload_preserves_hop_body(
    pytester, http_server, read_har
):
    payload = b"hop-payload" * 200
    upload = pytester.path / "payload.bin"
    upload.write_bytes(payload)

    pytester.makepyfile(
        """
        import requests

        def test_307_upload():
            with open({path!r}, "rb") as handle:
                response = requests.post(
                    {url!r} + "/redirect/307",
                    data=handle,
                    headers={{"Content-Type": "application/octet-stream"}},
                    timeout=5,
                )
            assert response.status_code == 200
            assert response.json()["sha256"] == {sha!r}
        """.format(
            path=str(upload),
            url=http_server,
            sha=hashlib.sha256(payload).hexdigest(),
        )
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    assert [entry["response"]["status"] for entry in entries] == [307, 200]
    for entry in entries:
        assert entry["request"]["bodySize"] == len(payload)
        post = entry["request"]["postData"]
        assert post["_file"]
        sidecar = pytester.path / "captured_traffic" / post["_file"]
        assert sidecar.read_bytes() == payload


def test_requests_streaming_body_is_collected(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import requests

        def test_stream():
            with requests.get(
                {url!r} + "/download?size=65536", stream=True, timeout=5
            ) as response:
                chunks = list(response.iter_content(8192))
            assert sum(len(chunk) for chunk in chunks) == 65536
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert "_streaming" not in entry
    content = entry["response"]["content"]
    assert content["size"] == 65536
    assert base64.b64decode(content["text"]) == b"x" * 65536


def test_requests_non_utf8_body_uses_sidecar(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import requests

        def test_latin1():
            response = requests.post(
                {url!r} + "/echo",
                data=b"caf\\xe9",
                headers={{"Content-Type": "text/plain; charset=iso-8859-1"}},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    post = entry["request"]["postData"]
    assert post["text"] == "café"
    assert post["_file"]
    sidecar = pytester.path / "captured_traffic" / post["_file"]
    assert sidecar.read_bytes() == b"caf\xe9"


def test_requests_redirect_loop_records_history(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import pytest
        import requests

        def test_loop():
            with pytest.raises(requests.exceptions.TooManyRedirects):
                requests.get({url!r} + "/redirect-loop", timeout=5)
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    assert len(entries) > 1
    assert all(entry["response"]["status"] == 302 for entry in entries)
    assert "TooManyRedirects" in entries[-1]["_error"]
    assert entries[-1]["_redirects_truncated"] is True


def test_requests_file_like_upload_is_captured(
    pytester, http_server, read_har
):
    payload = b"file-like-payload"
    upload = pytester.path / "payload.bin"
    upload.write_bytes(payload)

    pytester.makepyfile(
        """
        import hashlib
        import requests

        def test_upload():
            with open({path!r}, "rb") as handle:
                response = requests.post(
                    {url!r} + "/echo",
                    data=handle,
                    headers={{"Content-Type": "application/octet-stream"}},
                    timeout=5,
                )
            assert response.status_code == 200
            assert response.json()["sha256"] == {sha!r}
        """.format(
            path=str(upload),
            url=http_server,
            sha=hashlib.sha256(payload).hexdigest(),
        )
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    post = entry["request"]["postData"]
    assert post["_file"]
    assert entry["request"]["bodySize"] == len(payload)
    sidecar = pytester.path / "captured_traffic" / post["_file"]
    assert sidecar.read_bytes() == payload


def test_requests_nested_hook_request_is_captured(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import requests

        def test_hook():
            def hook(response, *args, **kwargs):
                if "nested=1" not in response.url:
                    requests.get(
                        {url!r} + "/echo?nested=1", timeout=5
                    )

            response = requests.get(
                {url!r} + "/echo",
                hooks={{"response": hook}},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    urls = [
        entry["request"]["url"]
        for entry in read_har(pytester)["log"]["entries"]
    ]
    assert any("nested=1" in url for url in urls)
    assert any(url.endswith("/echo") for url in urls)


def test_requests_generator_upload_is_captured(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import requests

        def chunks():
            yield b"abc"
            yield b"defg"

        def test_upload():
            response = requests.post(
                {url!r} + "/upload",
                data=chunks(),
                headers={{"Content-Type": "application/octet-stream"}},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    request = entry["request"]
    assert "_unreadable_body" not in request
    assert request["bodySize"] == 7
    sidecar = (
        pytester.path / "captured_traffic" / request["postData"]["_file"]
    )
    assert sidecar.read_bytes() == b"abcdefg"


def test_requests_streaming_response_cap(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import requests

        def test_cap():
            with requests.get(
                {url!r} + "/download?size=70000", stream=True, timeout=5
            ) as response:
                payload = b"".join(response.iter_content(4096))
            assert len(payload) == 70000
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess(
        "-q", "--capture-traffic", "--capture-max-body-bytes=1024"
    )

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    content = entry["response"]["content"]
    assert content["size"] == 1024
    assert content["_truncated"] is True
    assert content["_size"] == 70000
    assert entry["response"]["bodySize"] == -1


def test_requests_buffered_response_cap(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import requests

        def test_cap():
            response = requests.get(
                {url!r} + "/download?size=70000", timeout=5
            )
            assert len(response.content) == 70000
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess(
        "-q", "--capture-traffic", "--capture-max-body-bytes=1024"
    )

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    content = entry["response"]["content"]
    assert content["size"] == 1024
    assert content["_truncated"] is True
    assert content["_size"] == 70000
    assert entry["response"]["bodySize"] == -1


def test_requests_upload_cap_only_truncates_capture(
    pytester, http_server, read_har
):
    payload = b"x" * 4096
    upload = pytester.path / "payload.bin"
    upload.write_bytes(payload)

    pytester.makepyfile(
        """
        import requests

        def test_upload():
            with open({path!r}, "rb") as handle:
                response = requests.post(
                    {url!r} + "/upload",
                    data=handle,
                    headers={{"Content-Type": "application/octet-stream"}},
                    timeout=5,
                )
            body = response.json()
            assert body["length"] == 4096
            assert body["sha256"] == {digest!r}
        """.format(
            path=str(upload),
            url=http_server,
            digest=hashlib.sha256(payload).hexdigest(),
        )
    )

    result = pytester.runpytest_subprocess(
        "-q", "--capture-traffic", "--capture-max-body-bytes=1024"
    )

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    request = entry["request"]
    assert request["_truncated"] is True
    assert request["_size"] == 4096
    assert request["bodySize"] == 4096
    sidecar = (
        pytester.path / "captured_traffic" / request["postData"]["_file"]
    )
    assert len(sidecar.read_bytes()) == 1024


def test_requests_streaming_bodiless_responses_report_zero(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import requests

        def test_bodiless():
            session = requests.Session()
            try:
                session.get({url!r} + "/no-content", stream=True, timeout=5)
                session.get({url!r} + "/not-modified", stream=True, timeout=5)
                session.get({url!r} + "/empty", stream=True, timeout=5)
                session.head({url!r} + "/head", stream=True, timeout=5)
            finally:
                session.close()
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    assert len(entries) == 4
    for entry in entries:
        assert "_streaming" not in entry
        assert entry["response"]["content"]["size"] == 0
        assert entry["response"]["bodySize"] == 0


def test_requests_connection_error_is_marked_not_sent(
    pytester, closed_port, read_har
):
    pytester.makepyfile(
        """
        import pytest
        import requests

        def test_refused():
            with pytest.raises(requests.exceptions.ConnectionError):
                requests.get("http://127.0.0.1:{port}/x", timeout=5)
        """.format(port=closed_port)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert entry["request"]["_not_sent"] is True
    assert "ConnectionError" in entry["_error"]


def test_requests_multipart_text_field_respects_inline_cap(
    pytester, http_server, read_har
):
    blob = "x" * 512

    pytester.makepyfile(
        """
        import requests

        def test_upload():
            response = requests.post(
                {url!r} + "/multipart",
                data={{"field": "value"}},
                files={{"blob": (None, {blob!r})}},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server, blob=blob)
    )

    result = pytester.runpytest_subprocess(
        "-q", "--capture-traffic", "--capture-max-inline-bytes=64"
    )

    result.assert_outcomes(passed=1)
    request = read_har(pytester)["log"]["entries"][0]["request"]
    params = {
        item["name"]: item for item in request["postData"]["params"]
    }
    assert params["field"]["value"] == "value"
    assert params["blob"]["value"] == ""
    assert params["blob"]["_file"]
    sidecar = (
        pytester.path / "captured_traffic" / params["blob"]["_file"]
    )
    assert sidecar.read_bytes() == blob.encode("utf-8")


def test_requests_percent_filename_is_not_decoded(
    pytester, http_server, read_har
):
    """requests 原样发送文件名；字面 ``%XX`` 不能被当成编码还原。"""
    pytester.makepyfile(
        """
        import requests

        def test_upload():
            response = requests.post(
                {url!r} + "/multipart",
                files={{"file": ("50%20off.txt", b"abc")}},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    request = read_har(pytester)["log"]["entries"][0]["request"]
    params = {
        item["name"]: item for item in request["postData"]["params"]
    }
    assert params["file"]["fileName"] == "50%20off.txt"
