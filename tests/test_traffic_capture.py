"""pytest-traffic-capture 的端到端测试。"""

from __future__ import annotations

import json


def test_help_lists_options(pytester):
    result = pytester.runpytest_subprocess("--help")

    result.stdout.fnmatch_lines(
        [
            "HTTP traffic capture:",
            "*--capture-traffic*",
            "*--capture-dir=DIR*",
            "*--capture-max-inline-bytes=N*",
            "*--capture-max-body-bytes=N*",
        ]
    )


def test_invalid_max_body_bytes_is_usage_error(pytester):
    pytester.makepyfile("def test_ok(): pass")

    result = pytester.runpytest_subprocess(
        "-q", "--capture-traffic", "--capture-max-body-bytes=-1"
    )

    assert result.ret == 4
    result.stderr.fnmatch_lines(
        ["*capture_traffic_max_body_bytes must be >= 0*"]
    )


def test_capture_is_opt_in(pytester):
    pytester.makepyfile("def test_ok(): pass")

    result = pytester.runpytest_subprocess("-q")

    result.assert_outcomes(passed=1)
    assert not (pytester.path / "captured_traffic").exists()


def test_capture_writes_har_and_curl(pytester, http_server):
    pytester.makepyfile(
        """
        import requests

        def test_http():
            response = requests.get({url!r}, timeout=5)
            assert response.status_code == 200
        """.format(url=http_server + "/echo")
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*captured 1 exchange(s)*"])

    out = pytester.path / "captured_traffic"
    har_files = sorted(out.glob("*.har"))
    curl_files = sorted(out.glob("*.curl.txt"))
    assert len(har_files) == 1
    assert len(curl_files) == 1

    har = json.loads(har_files[0].read_text(encoding="utf-8"))
    entry = har["log"]["entries"][0]
    assert entry["request"]["method"] == "GET"
    assert entry["response"]["status"] == 200
    assert "curl " in curl_files[0].read_text(encoding="utf-8")


def test_capture_dir_is_not_collected_by_doctest(pytester):
    pytester.makepyfile("def test_ok(): pass")
    out = pytester.path / "captured_traffic"
    out.mkdir()
    (out / "test_poison.txt").write_text(
        ">>> assert 1 == 2\n", encoding="utf-8"
    )

    result = pytester.runpytest_subprocess("-q")

    result.assert_outcomes(passed=1)


def test_non_utf8_sidecar_does_not_break_collection(pytester):
    pytester.makepyfile("def test_ok(): pass")
    bodies = pytester.path / "captured_traffic" / "bodies"
    bodies.mkdir(parents=True)
    (bodies / "test_old_001_response.txt").write_bytes(b"\x80\x81")

    result = pytester.runpytest_subprocess("-q")

    result.assert_outcomes(passed=1)


def test_custom_capture_dir_is_not_collected_by_doctest(pytester):
    pytester.makepyfile("def test_ok(): pass")
    custom = pytester.path / "artifacts" / "traffic"
    custom.mkdir(parents=True)
    (custom / "test_poison.txt").write_text(
        ">>> assert 1 == 2\n", encoding="utf-8"
    )

    result = pytester.runpytest_subprocess(
        "-q", "--capture-dir", str(custom)
    )

    result.assert_outcomes(passed=1)


def test_capture_dir_parent_does_not_disable_collection(pytester):
    pytester.makepyfile("def test_ok(): pass")

    result = pytester.runpytest_subprocess("-q", "--capture-dir", "..")

    result.assert_outcomes(passed=1)


def test_captured_artifacts_do_not_leak_into_later_runs(pytester, http_server):
    pytester.makepyfile(
        """
        import requests

        def test_upload():
            response = requests.post(
                {url!r} + "/echo",
                data="first line\\n>>> assert 1 == 2\\nlast line",
                headers={{"Content-Type": "text/plain"}},
                timeout=5,
            )
            assert response.status_code == 200
        """.format(url=http_server)
    )

    first = pytester.runpytest_subprocess(
        "-q",
        "--capture-traffic",
        "--capture-max-inline-bytes=10",
    )
    first.assert_outcomes(passed=1)

    second = pytester.runpytest_subprocess("-q")

    second.assert_outcomes(passed=1)


def test_default_body_cap_truncates_large_bodies(pytester, http_server, read_har):
    size = 5 * 1024 * 1024 + 1
    pytester.makepyfile(
        """
        import requests

        def test_big():
            response = requests.get(
                {url!r} + "/download?size={size}", timeout=10
            )
            assert len(response.content) == {size}
        """.format(url=http_server, size=size)
    )

    result = pytester.runpytest_subprocess("-q", "--capture-traffic")

    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*truncated 1 body(ies)*"])
    entry = read_har(pytester)["log"]["entries"][0]
    content = entry["response"]["content"]
    assert content["size"] == 5 * 1024 * 1024
    assert content["_truncated"] is True
    assert content["_size"] == size
    assert entry["response"]["bodySize"] == -1
    # 头部不受 body 截断影响。
    assert entry["response"]["headers"]
    assert entry["request"]["headers"]


def test_zero_body_cap_disables_truncation(pytester, http_server, read_har):
    size = 5 * 1024 * 1024 + 1
    pytester.makepyfile(
        """
        import requests

        def test_big():
            response = requests.get(
                {url!r} + "/download?size={size}", timeout=10
            )
            assert len(response.content) == {size}
        """.format(url=http_server, size=size)
    )

    result = pytester.runpytest_subprocess(
        "-q", "--capture-traffic", "--capture-max-body-bytes=0"
    )

    result.assert_outcomes(passed=1)
    assert "truncated" not in result.stdout.str()
    content = read_har(pytester)["log"]["entries"][0]["response"]["content"]
    assert content["size"] == size
    assert "_truncated" not in content
