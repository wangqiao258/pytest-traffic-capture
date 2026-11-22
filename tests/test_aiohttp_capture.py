"""针对基于 aiohttp 的捕获路径的回归测试。"""

from __future__ import annotations

import hashlib

import pytest


def _aiohttp_version():
    try:
        import aiohttp
    except ImportError:
        return (0, 0)
    parts = aiohttp.__version__.split(".")
    return tuple(int(part) for part in parts[:2])


def test_aiohttp_307_preserves_body(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_307():
            async def scenario():
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        {url!r} + "/redirect/307", data=b"abc", timeout=5
                    ) as response:
                        assert response.status == 200

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    assert [entry["response"]["status"] for entry in entries] == [307, 200]
    assert entries[1]["request"]["method"] == "POST"
    assert entries[1]["request"]["bodySize"] == 3
    post = entries[1]["request"]["postData"]
    sidecar = pytester.path / "captured_traffic" / post["_file"]
    assert sidecar.read_bytes() == b"abc"


def test_aiohttp_readline_and_readuntil_still_work(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_lines():
            async def scenario():
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        {url!r} + "/lines", timeout=5
                    ) as response:
                        lines = [line async for line in response.content]
                assert lines == [b"hello\\n", b"world\\n"]

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert entry["response"]["content"]["size"] == 12
    assert entry["response"]["content"]["text"] == "hello\nworld\n"


def test_aiohttp_gzip_full_read_is_complete(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_gzip():
            async def scenario():
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        {url!r} + "/gzip?size=100000", timeout=5
                    ) as response:
                        payload = await response.read()
                assert len(payload) == 100000

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert "_streaming" not in entry
    assert entry["response"]["content"]["size"] == 100000
    assert entry["response"]["content"]["text"] == "A" * 100000


def test_aiohttp_gzip_partial_read_is_marked_streaming(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_gzip_partial():
            async def scenario():
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        {url!r} + "/gzip?size=100000", timeout=5
                    ) as response:
                        payload = await response.content.read(5000)
                assert len(payload) == 5000

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert entry["_streaming"] is True
    assert entry["response"]["content"]["size"] == -1
    assert entry["response"]["bodySize"] == -1


def test_aiohttp_readexactly_is_collected(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_exactly():
            async def scenario():
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        {url!r} + "/download?size=21&chunked=1", timeout=5
                    ) as response:
                        payload = await response.content.readexactly(21)
                        # 读完到 EOF，确保终止块已被解析；仅在 readexactly
                        # 后退出时，body 是否标记为 streaming 取决于分块时序。
                        await response.content.read()
                assert payload == b"x" * 21

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert "_streaming" not in entry
    assert entry["response"]["content"]["size"] == 21


def test_aiohttp_file_upload_is_captured(
    pytester, http_server, read_har
):
    payload = b"file-bytes" * 1024
    upload = pytester.path / "upload.bin"
    upload.write_bytes(payload)

    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_upload():
            async def scenario():
                with open({path!r}, "rb") as handle:
                    form = aiohttp.FormData()
                    form.add_field(
                        "file",
                        handle,
                        filename="upload.bin",
                        content_type="application/octet-stream",
                    )
                    async with aiohttp.ClientSession() as session:
                        async with session.post(
                            {url!r} + "/upload", data=form, timeout=5
                        ) as response:
                            payload = await response.json()
                assert payload["length"] > {size}

            asyncio.run(scenario())
        """.format(
            path=str(upload), url=http_server, size=len(payload)
        )
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    params = {item["name"]: item for item in entry["request"]["postData"]["params"]}
    assert params["file"]["fileName"] == "upload.bin"
    sidecar = pytester.path / "captured_traffic" / params["file"]["_file"]
    assert sidecar.read_bytes() == payload


def test_aiohttp_dict_bytes_is_captured_as_multipart(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_upload():
            async def scenario():
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        {url!r} + "/multipart",
                        data={{"raw": b"\\xff\\xfe"}},
                        timeout=5,
                    ) as response:
                        payload = await response.json()
                assert payload["parts"][0]["name"] == "raw"

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert entry["request"]["postData"]["mimeType"].startswith(
        "multipart/form-data"
    )
    params = entry["request"]["postData"]["params"]
    assert params[0]["name"] == "raw"
    assert params[0]["fileName"] == "raw"
    sidecar = pytester.path / "captured_traffic" / params[0]["_file"]
    assert sidecar.read_bytes() == b"\xff\xfe"


def test_aiohttp_pair_list_is_captured(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_form():
            async def scenario():
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        {url!r} + "/echo",
                        data=[("a", "1"), ("a", "2")],
                        timeout=5,
                    ) as response:
                        payload = await response.json()
                assert payload["body"] == "a=1&a=2"

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    post = read_har(pytester)["log"]["entries"][0]["request"]["postData"]
    assert post["mimeType"] == "application/x-www-form-urlencoded"
    assert [(item["name"], item["value"]) for item in post["params"]] == [
        ("a", "1"),
        ("a", "2"),
    ]


def test_aiohttp_callable_raise_for_status_records_response(
    pytester, http_server, read_har
):
    if _aiohttp_version() < (3, 10):
        pytest.skip("callable raise_for_status requires aiohttp >= 3.10")

    pytester.makepyfile(
        """
        import asyncio
        import aiohttp
        import pytest

        def test_hook():
            async def scenario():
                def hook(response):
                    raise RuntimeError("boom")

                async with aiohttp.ClientSession() as session:
                    with pytest.raises(RuntimeError):
                        async with session.get(
                            {url!r} + "/status/500",
                            raise_for_status=hook,
                            timeout=5,
                        ):
                            pass

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    assert entry["response"]["status"] == 500
    assert entry["response"]["headers"]


def test_aiohttp_timeout_keeps_automatic_headers(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp
        import pytest

        def test_timeout():
            async def scenario():
                timeout = aiohttp.ClientTimeout(total=0.2, sock_read=0.2)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    with pytest.raises(asyncio.TimeoutError):
                        await session.get({url!r} + "/slow?ms=1000")

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    headers = {
        header["name"].lower(): header["value"]
        for header in entry["request"]["headers"]
    }
    import aiohttp

    assert entry["request"]["_headers_synthesized"] is True
    assert "_not_sent" not in entry["request"]
    assert headers["accept-encoding"] == (
        aiohttp.ClientRequest.DEFAULT_HEADERS["Accept-Encoding"]
    )
    assert headers["user-agent"] == aiohttp.http.SERVER_SOFTWARE
    assert headers["host"] == http_server.split("//", 1)[1]


def test_aiohttp_redirect_loop_records_history(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp
        import pytest

        def test_loop():
            async def scenario():
                async with aiohttp.ClientSession() as session:
                    with pytest.raises(aiohttp.TooManyRedirects):
                        async with session.get(
                            {url!r} + "/redirect-loop", timeout=5
                        ):
                            pass

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entries = read_har(pytester)["log"]["entries"]
    assert len(entries) > 1
    assert all(entry["response"]["status"] == 302 for entry in entries)
    assert "TooManyRedirects" in entries[-1]["_error"]


def test_aiohttp_formdata_body_size_is_exact(
    pytester, http_server, read_har
):
    length_file = pytester.path / "length.txt"

    pytester.makepyfile(
        """
        import asyncio
        import aiohttp
        from pathlib import Path

        def test_upload():
            async def scenario():
                form = aiohttp.FormData()
                form.add_field("field", "value")
                form.add_field(
                    "file",
                    b"abc",
                    filename="a.txt",
                    content_type="text/plain",
                )
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        {url!r} + "/upload", data=form, timeout=5
                    ) as response:
                        body = await response.json()
                Path({length_path!r}).write_text(
                    str(body["length"]), encoding="utf-8"
                )

            asyncio.run(scenario())
        """.format(url=http_server, length_path=str(length_file))
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    expected = int(length_file.read_text(encoding="utf-8"))
    assert expected > 0
    assert entry["request"]["bodySize"] == expected


def test_aiohttp_unicode_filename_is_decoded(
    pytester, http_server, read_har
):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_upload():
            async def scenario():
                form = aiohttp.FormData()
                form.add_field(
                    "file",
                    b"abc",
                    filename="文件.txt",
                    content_type="text/plain",
                )
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        {url!r} + "/multipart", data=form, timeout=5
                    ) as response:
                        payload = await response.json()
                # aiohttp 在线上对文件名做百分号编码。
                assert payload["parts"][0]["filename"] == (
                    "%E6%96%87%E4%BB%B6.txt"
                )

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    entry = read_har(pytester)["log"]["entries"][0]
    params = entry["request"]["postData"]["params"]
    file_param = next(item for item in params if item.get("fileName"))
    assert file_param["fileName"] == "文件.txt"


def test_aiohttp_streaming_response_cap(pytester, http_server, read_har):
    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_cap():
            async def scenario():
                async with aiohttp.ClientSession() as session:
                    async with session.get(
                        {url!r} + "/download?size=70000", timeout=5
                    ) as response:
                        payload = await response.read()
                assert len(payload) == 70000

            asyncio.run(scenario())
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


def test_aiohttp_upload_cap_only_truncates_capture(
    pytester, http_server, read_har
):
    payload = b"y" * 4096
    upload = pytester.path / "payload.bin"
    upload.write_bytes(payload)

    pytester.makepyfile(
        """
        import asyncio
        import aiohttp

        def test_upload():
            async def scenario():
                async with aiohttp.ClientSession() as session:
                    with open({path!r}, "rb") as handle:
                        async with session.post(
                            {url!r} + "/upload",
                            data=handle,
                            headers={{"Content-Type": "application/octet-stream"}},
                        ) as response:
                            body = await response.json()
                assert body["length"] == 4096
                assert body["sha256"] == {digest!r}

            asyncio.run(scenario())
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
