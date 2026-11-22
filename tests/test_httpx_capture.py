"""针对基于 httpx 的捕获路径的回归测试。"""

from __future__ import annotations

import base64
import hashlib

import httpx


def test_httpx_file_upload_is_captured(
    pytester, http_server, read_har
):
    payload = b"\xff\xfe" + b"httpx-file" * 100
    upload = pytester.path / "payload.bin"
    upload.write_bytes(payload)

    pytester.makepyfile(
        """
        import httpx

        def test_upload():
            with open({path!r}, "rb") as handle:
                response = httpx.post(
                    {url!r} + "/upload",
                    content=handle,
                    headers={{"Content-Type": "application/octet-stream"}},
                    timeout=5,
                )
            assert response.json()["sha256"] == {digest!r}

        """.format(
            path=str(upload),
            url=http_server,
            digest=hashlib.sha256(payload).hexdigest(),
        )
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert entry["request"]["bodySize"] == len(payload)
    sidecar = (
        pytester.path
        / "captured_traffic"
        / entry["request"]["postData"]["_file"]
    )
    assert sidecar.read_bytes() == payload


def test_httpx_307_preserves_body(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import httpx

        def test_307():
            with httpx.Client() as client:
                response = client.post(
                    {url!r} + "/redirect/307",
                    content=b"abc",
                    follow_redirects=True,
                    timeout=5,
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


def test_httpx_get_redirect_has_no_unreadable_body(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import httpx

        def test_redirect():
            with httpx.Client() as client:
                response = client.get(
                    {url!r} + "/redirect/302",
                    follow_redirects=True,
                    timeout=5,
                )
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    assert len(entries) == 2
    assert "_unreadable_body" not in entries[1]["request"]
    assert entries[1]["request"]["bodySize"] == 0


def test_httpx_streaming_body_is_collected(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import httpx

        def test_stream():
            with httpx.Client() as client:
                with client.stream(
                    "GET",
                    {url!r} + "/download?size=65536",
                    timeout=5,
                ) as response:
                    payload = b"".join(response.iter_bytes())
            assert len(payload) == 65536
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert "_streaming" not in entry
    content = entry["response"]["content"]
    assert content["size"] == 65536
    assert base64.b64decode(content["text"]) == b"x" * 65536


def test_httpx_redirect_loop_records_history(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import httpx
        import pytest

        def test_loop():
            with httpx.Client(follow_redirects=True, timeout=5) as client:
                with pytest.raises(httpx.TooManyRedirects):
                    client.get({url!r} + "/redirect-loop")
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    with httpx.Client() as client:
        expected = client.max_redirects + 1
    assert len(entries) == expected
    assert all(entry["response"]["status"] == 302 for entry in entries)
    assert "TooManyRedirects" in entries[-1]["_error"]
    assert entries[-1]["_redirects_truncated"] is True
    assert all("_not_sent" not in entry["request"] for entry in entries)


def test_httpx_async_redirect_loop_records_history(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import asyncio
        import httpx
        import pytest

        def test_loop():
            async def scenario():
                async with httpx.AsyncClient(
                    follow_redirects=True, timeout=5
                ) as client:
                    with pytest.raises(httpx.TooManyRedirects):
                        await client.get({url!r} + "/redirect-loop")

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    with httpx.Client() as client:
        expected = client.max_redirects + 1
    assert len(entries) == expected
    assert all(entry["response"]["status"] == 302 for entry in entries)
    assert "TooManyRedirects" in entries[-1]["_error"]
    assert entries[-1]["_redirects_truncated"] is True
    assert all("_not_sent" not in entry["request"] for entry in entries)


def test_httpx_upload_cap_only_truncates_capture(
    pytester, http_server, read_har
):
    payload = b"z" * 4096
    upload = pytester.path / "payload.bin"
    upload.write_bytes(payload)

    pytester.makepyfile(
        """
        import httpx

        def test_upload():
            with open({path!r}, "rb") as handle:
                response = httpx.post(
                    {url!r} + "/upload",
                    content=handle,
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


def test_httpx_async_streaming_body_is_collected(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import asyncio
        import httpx

        def test_stream():
            async def scenario():
                async with httpx.AsyncClient() as client:
                    async with client.stream(
                        "GET",
                        {url!r} + "/download?size=65536",
                        timeout=5,
                    ) as response:
                        return b"".join(
                            [chunk async for chunk in response.aiter_bytes()]
                        )

            payload = asyncio.run(scenario())
            assert len(payload) == 65536
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert "_streaming" not in entry
    content = entry["response"]["content"]
    assert content["size"] == 65536
    assert base64.b64decode(content["text"]) == b"x" * 65536


def test_httpx_streaming_bodiless_responses_report_zero(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import httpx

        def test_bodiless():
            with httpx.Client() as client:
                with client.stream(
                    "GET", {url!r} + "/no-content", timeout=5
                ):
                    pass
                with client.stream(
                    "GET", {url!r} + "/not-modified", timeout=5
                ):
                    pass
                with client.stream(
                    "GET", {url!r} + "/empty", timeout=5
                ):
                    pass
                with client.stream(
                    "HEAD", {url!r} + "/head", timeout=5
                ):
                    pass
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


def test_httpx_connection_error_is_marked_not_sent(
    pytester, closed_port, read_har
):
    pytester.makepyfile(
        """
        import httpx
        import pytest

        def test_refused():
            with pytest.raises(httpx.ConnectError):
                httpx.get("http://127.0.0.1:{port}/x", timeout=5)
        """.format(port=closed_port)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert entry["request"]["_not_sent"] is True
    assert "ConnectError" in entry["_error"]


def test_httpx_streaming_upload_is_not_buffered(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import httpx

        def test_upload():
            def chunks():
                yield b"abc"
                yield b"defg"

            request = httpx.Request(
                "POST",
                {url!r} + "/upload",
                content=chunks(),
                headers={{"Content-Type": "application/octet-stream"}},
            )
            with httpx.Client() as client:
                response = client.send(request)
            assert response.status_code == 200
            assert not hasattr(request, "_content")
            assert not isinstance(request.stream, httpx.ByteStream)

        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    request = read_har(pytester)["log"]["entries"][0]["request"]
    assert request["bodySize"] == 7
    sidecar = (
        pytester.path / "captured_traffic" / request["postData"]["_file"]
    )
    assert sidecar.read_bytes() == b"abcdefg"


def test_httpx_multipart_upload_is_captured(pytester, http_server, read_har):
    payload = b"httpx-multipart-payload"

    pytester.makepyfile(
        """
        import httpx

        def test_upload():
            response = httpx.post(
                {url!r} + "/upload",
                files={{"file": ("a.bin", {payload!r})}},
                timeout=5,
            )
            assert response.status_code == 200

        """.format(url=http_server, payload=payload)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    request = read_har(pytester)["log"]["entries"][0]["request"]
    params = {
        item["name"]: item for item in request["postData"]["params"]
    }
    assert params["file"]["fileName"] == "a.bin"
    assert params["file"]["_file"]
    sidecar = (
        pytester.path / "captured_traffic" / params["file"]["_file"]
    )
    assert sidecar.read_bytes() == payload


def test_httpx_async_streaming_upload_is_captured(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import asyncio
        import httpx

        def test_upload():
            async def chunks():
                yield b"abc"
                yield b"defg"

            async def scenario():
                async with httpx.AsyncClient() as client:
                    request = httpx.Request(
                        "POST",
                        {url!r} + "/upload",
                        content=chunks(),
                        headers={{"Content-Type": "application/octet-stream"}},
                    )
                    response = await client.send(request)
                assert response.status_code == 200

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    request = read_har(pytester)["log"]["entries"][0]["request"]
    assert request["bodySize"] == 7
    sidecar = (
        pytester.path / "captured_traffic" / request["postData"]["_file"]
    )
    assert sidecar.read_bytes() == b"abcdefg"
