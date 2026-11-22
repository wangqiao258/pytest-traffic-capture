"""针对本地服务器回放生成的 cURL 命令。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess

import pytest

_GIT_BASH = r"C:\Program Files\Git\bin\bash.exe"


def _bash() -> str:
    return shutil.which("bash") or (_GIT_BASH if os.path.exists(_GIT_BASH) else "")


def _commands(curl_text: str):
    return [
        line.strip()
        for line in curl_text.splitlines()
        if line.strip().startswith("curl ")
    ]


def _replay(curl_text: str, index: int = -1) -> dict:
    bash = _bash()
    if not bash:
        pytest.skip("bash is not available")
    command = _commands(curl_text)[index]
    result = subprocess.run(
        [bash, "-c", command],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_aiohttp_307_curl_replays_original_body(
    pytester, http_server, read_curl
):
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

    payload = _replay(read_curl(pytester))
    assert payload["method"] == "POST"
    assert payload["size"] == 3
    assert payload["sha256"] == hashlib.sha256(b"abc").hexdigest()


def test_aiohttp_unicode_filename_curl_replay_preserves_wire_name(
    pytester, http_server, read_curl
):
    pytester.makepyfile(
        test_unicode_upload="""
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
                        assert response.status == 200

            asyncio.run(scenario())
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")
    result.assert_outcomes(passed=1)

    payload = _replay(read_curl(pytester))
    assert payload["parts"][0]["filename"] == "%E6%96%87%E4%BB%B6.txt"


def test_non_utf8_curl_replays_identical_bytes(
    pytester, http_server, read_curl
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

    payload = _replay(read_curl(pytester))
    assert payload["size"] == 4
    assert payload["sha256"] == hashlib.sha256(b"caf\xe9").hexdigest()


def test_non_ascii_curl_replays_identical_bytes(
    pytester, http_server, read_curl
):
    body = "中文请求体".encode("utf-8")
    pytester.makepyfile(
        """
        import requests

        def test_unicode():
            response = requests.post(
                {url!r} + "/echo",
                data={body!r},
                headers={{"Content-Type": "text/plain; charset=utf-8"}},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server, body=body)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")
    result.assert_outcomes(passed=1)

    payload = _replay(read_curl(pytester))
    assert payload["size"] == len(body)
    assert payload["sha256"] == hashlib.sha256(body).hexdigest()


def test_nul_body_curl_replays_identical_bytes(
    pytester, http_server, read_curl
):
    body = b"abc\x00def"
    pytester.makepyfile(
        """
        import requests

        def test_nul():
            response = requests.post(
                {url!r} + "/echo",
                data={body!r},
                headers={{"Content-Type": "text/plain"}},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server, body=body)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")
    result.assert_outcomes(passed=1)

    payload = _replay(read_curl(pytester))
    assert payload["size"] == len(body)
    assert payload["sha256"] == hashlib.sha256(body).hexdigest()


def test_multipart_fields_curl_replay_preserves_types(
    pytester, http_server, read_curl
):
    pytester.makepyfile(
        """
        import requests

        def test_multipart():
            response = requests.post(
                {url!r} + "/multipart",
                files={{
                    "jsonfield": (None, '{{"a": 1}}', "application/json"),
                    "binfield": (None, b"\\xff\\xfe\\x00binary"),
                    "at": (None, "@/etc/passwd"),
                    "note": (
                        "note.txt",
                        b"hello",
                        "text/plain; charset=utf-8",
                    ),
                }},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")
    result.assert_outcomes(passed=1)

    payload = _replay(read_curl(pytester))
    parts = {part["name"]: part for part in payload["parts"]}

    assert parts["jsonfield"]["content_type"] == "application/json"
    assert parts["jsonfield"]["sha256"] == hashlib.sha256(b'{"a": 1}').hexdigest()
    assert parts["binfield"]["content_type"] is None
    assert parts["binfield"]["sha256"] == hashlib.sha256(
        b"\xff\xfe\x00binary"
    ).hexdigest()
    assert parts["at"]["content_type"] is None
    assert parts["at"]["sha256"] == hashlib.sha256(b"@/etc/passwd").hexdigest()
    assert parts["note"]["content_type"].replace('"', "") == (
        "text/plain; charset=utf-8"
    )
    assert parts["note"]["filename"] == "note.txt"
    assert parts["note"]["sha256"] == hashlib.sha256(b"hello").hexdigest()


def test_multipart_unsafe_field_name_curl_replays(pytester, http_server, read_curl):
    pytester.makepyfile(
        """
        import requests

        def test_unsafe_name():
            response = requests.post(
                {url!r} + "/multipart",
                files={{"a=b": (None, "value")}},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")
    result.assert_outcomes(passed=1)

    curl_text = read_curl(pytester)
    assert "--data-binary '@" in curl_text
    payload = _replay(curl_text)
    parts = {part["name"]: part for part in payload["parts"]}
    assert parts["a=b"]["sha256"] == hashlib.sha256(b"value").hexdigest()


def test_curl_suppresses_default_content_type(pytester, http_server, read_curl):
    body = b"\x00\x01binary\xff"
    pytester.makepyfile(
        """
        import requests

        def test_no_content_type():
            response = requests.post(
                {url!r} + "/echo",
                data={body!r},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server, body=body)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")
    result.assert_outcomes(passed=1)

    curl_text = read_curl(pytester)
    assert "-H 'Content-Type:'" in curl_text
    payload = _replay(curl_text)
    assert "Content-Type" not in payload["headers"]
    assert payload["sha256"] == hashlib.sha256(body).hexdigest()


def test_multiline_body_curl_replays(pytester, http_server, read_curl):
    body = "first line\n>>> assert 1 == 2\nlast line"
    pytester.makepyfile(
        """
        import requests

        def test_multiline():
            response = requests.post(
                {url!r} + "/echo",
                data={body!r},
                headers={{"Content-Type": "text/plain"}},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server, body=body)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")
    result.assert_outcomes(passed=1)

    curl_text = read_curl(pytester)
    assert not any(
        line.strip().startswith(">>>") for line in curl_text.splitlines()
    )
    payload = _replay(curl_text)
    assert payload["sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
