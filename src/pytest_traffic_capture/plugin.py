"""pytest 插件接线：选项、钩子与产物输出。"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import pytest

from . import adapters
from .recorder import Recorder

_RECORDER_KEY = pytest.StashKey[Recorder]()


def pytest_addoption(parser) -> None:
    group = parser.getgroup("traffic-capture", "HTTP traffic capture")
    group.addoption(
        "--capture-traffic",
        action="store_true",
        default=False,
        help="Capture requests/httpx/aiohttp traffic as HAR 1.2 and cURL commands.",
    )
    group.addoption(
        "--capture-dir",
        action="store",
        default=None,
        metavar="DIR",
        help="Output directory for captured traffic (default: capture_traffic_dir ini).",
    )
    group.addoption(
        "--capture-max-inline-bytes",
        action="store",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Maximum body size kept inline in the HAR file; larger bodies go "
            "to sidecar files (0 disables the limit for textual request bodies "
            "and response bodies)."
        ),
    )
    group.addoption(
        "--capture-max-body-bytes",
        action="store",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Maximum bytes captured per request or response body; larger "
            "bodies are truncated and marked (0 disables the limit)."
        ),
    )
    parser.addini(
        "capture_traffic_dir",
        "Output directory used by --capture-traffic.",
        default="captured_traffic",
    )
    parser.addini(
        "capture_traffic_max_inline_bytes",
        "Maximum response body size kept inline in the HAR file.",
        default="1048576",
    )
    parser.addini(
        "capture_traffic_max_body_bytes",
        "Maximum bytes captured per request or response body.",
        default="0",
    )


def _resolve_output_dir(config) -> Path:
    configured = config.getoption("capture_dir") or config.getini("capture_traffic_dir")
    raw = str(configured or "").strip() or "captured_traffic"
    path = Path(os.path.expanduser(raw))
    if not path.is_absolute():
        path = Path(str(config.rootpath)) / path
    return path


def _resolve_max_inline(config) -> int:
    value = config.getoption("capture_max_inline_bytes")
    if value is None:
        value = config.getini("capture_traffic_max_inline_bytes") or "1048576"
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise pytest.UsageError(
            "capture_traffic_max_inline_bytes must be an integer, got {!r}".format(
                value
            )
        )
    if parsed < 0:
        raise pytest.UsageError(
            "capture_traffic_max_inline_bytes must be >= 0, got {}".format(parsed)
        )
    return parsed


def _resolve_max_body_bytes(config) -> int:
    value = config.getoption("capture_max_body_bytes")
    if value is None:
        value = config.getini("capture_traffic_max_body_bytes") or "0"
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise pytest.UsageError(
            "capture_traffic_max_body_bytes must be an integer, got {!r}".format(
                value
            )
        )
    if parsed < 0:
        raise pytest.UsageError(
            "capture_traffic_max_body_bytes must be >= 0, got {}".format(parsed)
        )
    return parsed


def _recorder(config) -> Optional[Recorder]:
    return config.stash.get(_RECORDER_KEY, None)


@pytest.hookimpl(tryfirst=True)
def pytest_ignore_collect(collection_path, config):
    """让捕获产物不进入 pytest 收集。

    内置的 doctest 插件会收集每一个 ``test*.txt`` 文件，而默认的捕获目录
    位于 ``rootpath`` 之内。在这里忽略它，还能保护未启用
    ``--capture-traffic`` 的运行，使其不受先前捕获运行遗留产物的影响。
    """
    try:
        target = _resolve_output_dir(config).resolve()
        root = Path(str(config.rootpath)).resolve()
        path = Path(str(collection_path)).resolve()
    except OSError:
        return None
    if target == root or target in root.parents:
        # 绝不忽略项目根目录（或其祖先目录）；产物名称使用
        # ``traffic_`` 前缀，因此默认的 doctest glob 仍然无法
        # 匹配到它们。
        return None
    if path == target or target in path.parents:
        return True
    return None


def pytest_configure(config) -> None:
    if not config.getoption("capture_traffic"):
        return
    recorder = Recorder(
        _resolve_output_dir(config),
        _resolve_max_inline(config),
        max_body_bytes=_resolve_max_body_bytes(config),
    )
    config.stash[_RECORDER_KEY] = recorder
    adapters.install(recorder)


def pytest_unconfigure(config) -> None:
    recorder = config.stash.get(_RECORDER_KEY, None)
    if recorder is not None:
        adapters.uninstall(recorder)


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_setup(item):
    recorder = _recorder(item.config)
    if recorder is not None:
        recorder.start_test(item.nodeid)
    return (yield)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item, nextitem):
    try:
        return (yield)
    finally:
        recorder = _recorder(item.config)
        if recorder is not None:
            recorder.finish_test()


def pytest_terminal_summary(terminalreporter, exitstatus, config) -> None:
    recorder = _recorder(config)
    if recorder is None:
        return
    if not recorder.total_exchanges and not recorder.write_failures:
        return
    terminalreporter.write_sep("-", "traffic capture")
    if recorder.total_exchanges:
        terminalreporter.write_line(
            "captured {} exchange(s) into {}".format(
                recorder.total_exchanges, recorder.out_dir
            )
        )
    if recorder.write_failures:
        terminalreporter.write_line(
            "warning: failed to write artifacts for {} test(s); "
            "see warnings above".format(recorder.write_failures)
        )
