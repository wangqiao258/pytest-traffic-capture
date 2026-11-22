"""线程安全的记录器，持有每个测试的交换缓冲。"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import tempfile
import threading
import warnings
from pathlib import Path
from typing import List, Optional, Tuple

from .exporters import build_curl, build_har
from .model import Exchange

_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|\s]+')


def _warn(message: str) -> None:
    """发出警告，但不让 ``-W error`` 把捕获失败升级为错误。"""
    try:
        warnings.warn(message, RuntimeWarning, stacklevel=3)
    except Warning:
        pass


def safe_nodeid(nodeid: str) -> str:
    """将 pytest nodeid 转换为带短哈希的文件系统安全名称。

    ``traffic_`` 前缀使基本名称不会匹配 pytest 默认的 doctest glob
    ``test*.txt``，即使捕获目录就是 rootdir 也是如此。
    """
    digest = hashlib.sha1(nodeid.encode("utf-8")).hexdigest()[:8]
    cleaned = _UNSAFE_CHARS.sub("_", nodeid).strip("._")
    cleaned = cleaned[:72].strip("._")
    return "traffic_{}_{}".format(cleaned or "test", digest)


def _safe_hint(hint: str) -> str:
    cleaned = _UNSAFE_CHARS.sub("_", str(hint or "body")).strip(" ._")
    cleaned = cleaned[:60].strip(" ._")
    return cleaned or "body"


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """将 *data* 写入 *path*，不留下写了一半的产物。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=str(path.parent),
        prefix=".tfc-",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """以原子写方式将 *text* 写入 *path*，保留体中的 ``\\r\\n``。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        dir=str(path.parent),
        prefix=".tfc-",
        suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temp_path), str(path))
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


class _TestSession:
    def __init__(self, nodeid: str, out_dir: Path) -> None:
        self.nodeid = nodeid
        self.out_dir = out_dir
        self.safe_name = safe_nodeid(nodeid)
        self.exchanges: List[Exchange] = []
        self._counter = 0
        self._lock = threading.Lock()

    @property
    def har_path(self) -> Path:
        return self.out_dir / "{}.har".format(self.safe_name)

    @property
    def curl_path(self) -> Path:
        return self.out_dir / "{}.curl.txt".format(self.safe_name)

    def cleanup_previous(self) -> None:
        """删除同一测试先前运行遗留的产物。"""
        for path in (self.har_path, self.curl_path):
            try:
                path.unlink()
            except OSError:
                pass
        body_dir = self.out_dir / "bodies"
        if not body_dir.is_dir():
            return
        for path in body_dir.glob("{}_*".format(glob.escape(self.safe_name))):
            try:
                path.unlink()
            except OSError:
                pass

    def add(self, exchange: Exchange) -> None:
        with self._lock:
            self.exchanges.append(exchange)

    def save_body(self, hint: str, data: bytes) -> str:
        with self._lock:
            self._counter += 1
            index = self._counter
        relative = "bodies/{}_{:03d}_{}".format(
            self.safe_name, index, _safe_hint(hint)
        )
        target = self.out_dir / Path(relative)
        try:
            _atomic_write_bytes(target, bytes(data))
        except Exception as exc:
            # 单个无法写入的旁挂文件(sidecar)（名称非法、路径过长、磁盘已满）
            # 不应导致整个 HAR/cURL 产物丢失。
            _warn(
                "pytest-traffic-capture: failed to write sidecar {}: {}".format(
                    relative, exc
                )
            )
            return ""
        return relative


class Recorder:
    """一次收集一个测试的交换，并在结束时写出产物。"""

    def __init__(
        self,
        out_dir,
        max_inline_bytes: int = 1048576,
        max_body_bytes: int = 0,
    ) -> None:
        self.out_dir = Path(out_dir)
        self.max_inline_bytes = int(max_inline_bytes)
        self.max_body_bytes = max(0, int(max_body_bytes))
        self._lock = threading.RLock()
        self._session: Optional[_TestSession] = None
        self._total_exchanges = 0
        self.write_failures = 0

    @property
    def total_exchanges(self) -> int:
        return self._total_exchanges

    def start_test(self, nodeid: str) -> None:
        session = _TestSession(nodeid, self.out_dir)
        session.cleanup_previous()
        with self._lock:
            self._session = session

    def is_capturing(self) -> bool:
        return self._session is not None

    def record(self, exchange: Exchange) -> None:
        with self._lock:
            session = self._session
        if session is not None:
            session.add(exchange)

    def save_body(self, hint: str, data: bytes) -> str:
        with self._lock:
            session = self._session
        if session is None:
            raise RuntimeError("pytest_traffic_capture: no active test session")
        return session.save_body(hint, data)

    def finish_test(self) -> Optional[Tuple[Path, Path]]:
        with self._lock:
            session = self._session
            self._session = None
        if session is None:
            return None
        if not session.exchanges:
            self._clear_lazy_bodies()
            return None

        try:
            har = build_har(
                session.exchanges,
                max_inline_bytes=self.max_inline_bytes,
                save_body=session.save_body,
            )
            curl = build_curl(
                session.exchanges,
                save_body=session.save_body,
                max_inline_bytes=self.max_inline_bytes,
                body_path_base=self.out_dir,
            )

            # newline="" 在 Windows 上保持请求体中的 ``\n`` 和 ``\r\n`` 原样，
            # 使生成的 cURL 命令重放完全相同的字节。
            _atomic_write_text(
                session.har_path,
                json.dumps(har, ensure_ascii=False, indent=2),
            )
            _atomic_write_text(session.curl_path, curl)
        except Exception as exc:
            with self._lock:
                self.write_failures += 1
            _warn(
                "pytest-traffic-capture: failed to write artifacts for {}: {}".format(
                    session.nodeid, exc
                )
            )
            return None
        finally:
            self._clear_lazy_bodies()

        with self._lock:
            self._total_exchanges += len(session.exchanges)
        return session.har_path, session.curl_path

    @staticmethod
    def _clear_lazy_bodies() -> None:
        try:
            from . import adapters

            adapters.clear_stream_collectors()
        except Exception:
            pass
