"""对 HTTP 客户端库打补丁(monkeypatch)，使其将交换上报给记录器。"""

from __future__ import annotations

import contextvars
import inspect
import io
import json
import os
import stat
import time
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Tuple
from urllib.parse import urlencode

from .model import Exchange, RequestRecord, ResponseRecord
from .multipart import Part

_recorders: List[Any] = []
_originals: List[Tuple[Any, str, Any]] = []
_installed = False

# 防止被包装的调用递归触发（requests 通过再次调用 Session.send 来解析重定向）。
# ContextVar 让并发的异步任务彼此独立。
_depth: contextvars.ContextVar = contextvars.ContextVar(
    "pytest_traffic_capture_depth", default=0
)

# httpx 在 Client.send 内部解析重定向。它的 TooManyRedirects 异常
# 只携带下一个从未发送的请求，因此改用 ContextVar 收集真正
# 到达传输层的各跳。
_httpx_hops: contextvars.ContextVar = contextvars.ContextVar(
    "pytest_traffic_capture_httpx_hops", default=None
)

# aiohttp 在 ClientRequest 内部构建最终的 multipart 载荷（包括其 boundary）；
# 这个 ContextVar 把测得的编码后大小传回请求记录。
# 未捕获时，哨兵让该补丁不起作用。
_AIOHTTP_SIZE_UNSET = object()
_aiohttp_body_size: contextvars.ContextVar = contextvars.ContextVar(
    "pytest_traffic_capture_aiohttp_body_size",
    default=_AIOHTTP_SIZE_UNSET,
)

# 需要惰性捕获其载荷的 aiohttp 响应读取器。我们绝不能
# 自己读取响应体：那样会破坏 iter_chunked()、SSE/长轮询
# 以及任务取消。改为注册该读取器，并在*调用方*
# 消费其字节时收集这些字节。
_reader_collectors: dict = {}


class _BodyCollector:
    __slots__ = (
        "chunks",
        "eof",
        "started",
        "cached",
        "reader",
        "limit",
        "total",
        "captured",
        "truncated",
    )

    def __init__(self, limit: int = 0) -> None:
        self.chunks: List[bytes] = []
        self.eof = False
        self.started = False
        self.cached: Optional[bytes] = None
        self.reader = None
        self.limit = max(0, int(limit or 0))
        self.total = 0
        self.captured = 0
        self.truncated = False

    def append(self, data: bytes) -> None:
        data = bytes(data)
        self.total += len(data)
        if not data:
            return
        if self.limit > 0:
            if self.captured >= self.limit:
                self.truncated = True
                return
            remaining = self.limit - self.captured
            if len(data) > remaining:
                data = data[:remaining]
                self.truncated = True
        self.chunks.append(data)
        self.captured += len(data)


def _collector_body(collector: _BodyCollector) -> Optional[bytes]:
    """在调用方完全消费后返回收集到的体。"""
    if not collector.eof:
        return None
    if collector.cached is None:
        collector.cached = b"".join(collector.chunks)
    if collector.total > collector.captured:
        collector.truncated = True
    return collector.cached


def _body_provider(collector: _BodyCollector):
    def provide() -> Optional[bytes]:
        body = _collector_body(collector)
        provide.truncated = collector.truncated
        # 只有当调用方把体消费到 EOF 时，``total`` 才是完整大小。
        # 部分消费不得被当作完整大小上报。
        provide.full_body_size = (
            collector.total
            if body is not None and collector.truncated
            else None
        )
        return body

    provide.truncated = False
    provide.full_body_size = None
    return provide


# --------------------------------------------------------------------------- #
# 安装 / 卸载
# --------------------------------------------------------------------------- #
def install(recorder) -> None:
    """为每个可用的客户端库打补丁。可安全地重复调用。"""
    global _installed
    if recorder in _recorders:
        return
    _recorders.append(recorder)
    if _installed:
        return
    _patch_requests()
    _patch_httpx()
    _patch_aiohttp()
    _patch_aiohttp_streams()
    _installed = True


def uninstall(recorder=None) -> None:
    """在最后一个记录器被移除后还原被打补丁的方法。"""
    global _installed
    if not _recorders:
        return
    if recorder is None:
        _recorders.clear()
    else:
        try:
            _recorders.remove(recorder)
        except ValueError:
            pass
    if _recorders:
        return
    while _originals:
        owner, attribute, original = _originals.pop()
        try:
            setattr(owner, attribute, original)
        except Exception:
            pass
    _installed = False
    _reader_collectors.clear()


def _active():
    return _recorders[-1] if _recorders else None


def _body_limit() -> int:
    recorder = _active()
    try:
        return max(0, int(getattr(recorder, "max_body_bytes", 0) or 0))
    except Exception:
        return 0


def _warn(message: str) -> None:
    try:
        warnings.warn(message, RuntimeWarning, stacklevel=3)
    except Warning:
        pass


def clear_stream_collectors() -> None:
    _reader_collectors.clear()


def _patch(owner, attribute: str, factory) -> None:
    try:
        original = getattr(owner, attribute)
    except Exception as exc:
        _warn(
            "pytest-traffic-capture: cannot patch {}.{}: {}".format(
                getattr(owner, "__name__", owner), attribute, exc
            )
        )
        return
    try:
        setattr(owner, attribute, factory(original))
    except Exception as exc:
        _warn(
            "pytest-traffic-capture: failed to patch {}.{}: {}".format(
                getattr(owner, "__name__", owner), attribute, exc
            )
        )
        return
    _originals.append((owner, attribute, original))


def _patch_requests() -> None:
    try:
        from requests.sessions import Session
    except Exception:
        return
    _patch(Session, "send", _make_requests_send)


def _patch_httpx() -> None:
    try:
        import httpx
    except Exception:
        return
    _patch(httpx.Client, "send", _make_httpx_send)
    _patch(httpx.AsyncClient, "send", _make_httpx_async_send)
    # 记录每一跳，使重定向循环保留整条重定向链，而不是只保留
    # TooManyRedirects 暴露的那个未发送的请求。
    _patch(
        httpx.Client,
        "_send_single_request",
        _make_httpx_single_request,
    )
    _patch(
        httpx.AsyncClient,
        "_send_single_request",
        _make_httpx_async_single_request,
    )


def _patch_aiohttp() -> None:
    try:
        from aiohttp import ClientRequest, ClientSession
    except Exception:
        return
    _patch(ClientSession, "_request", _make_aiohttp_request)
    _patch(ClientRequest, "__init__", _make_aiohttp_client_request_init)


def _patch_aiohttp_streams() -> None:
    try:
        from aiohttp.streams import StreamReader
    except Exception:
        return
    # ``_read_nowait_chunk`` 是 read()、readany()、readuntil()/readline()
    # 和 readchunk() 共用的唯一底层原语；给它打补丁可恰好捕获
    # 每一条消费路径一次。
    _patch(StreamReader, "_read_nowait_chunk", _make_read_nowait_chunk)
    _patch(StreamReader, "read", _make_reader_read)
    _patch(StreamReader, "readany", _make_reader_readany)
    _patch(StreamReader, "readuntil", _make_reader_readuntil)
    _patch(StreamReader, "readchunk", _make_reader_readchunk)


def _mark_reader_eof(reader) -> None:
    if not getattr(reader, "_eof", False):
        return
    if getattr(reader, "_buffer", None):
        return
    entry = _reader_collectors.get(id(reader))
    if entry is not None:
        entry[1].eof = True


def _reader_at_eof(reader) -> bool:
    if reader is None:
        return False
    try:
        return bool(getattr(reader, "_eof", False)) and not getattr(
            reader, "_buffer", None
        )
    except Exception:
        return False


def _make_read_nowait_chunk(original):
    def _read_nowait_chunk(self, n):
        data = original(self, n)
        if data:
            entry = _reader_collectors.get(id(self))
            if entry is not None:
                entry[1].append(bytes(data))
        _mark_reader_eof(self)
        return data

    return _read_nowait_chunk


def _make_reader_read(original):
    async def read(self, n=-1):
        data = await original(self, n)
        _mark_reader_eof(self)
        return data

    return read


def _make_reader_readany(original):
    async def readany(self):
        data = await original(self)
        if not data:
            _mark_reader_eof(self)
        return data

    return readany


def _make_reader_readuntil(original):
    async def readuntil(self, separator=b"\n", *, max_size=None):
        try:
            if max_size is None:
                data = await original(self, separator)
            else:
                data = await original(self, separator, max_size=max_size)
        except BaseException:
            _mark_reader_eof(self)
            raise
        _mark_reader_eof(self)
        return data

    return readuntil


def _make_reader_readchunk(original):
    async def readchunk(self):
        data, end_of_chunk = await original(self)
        if not data:
            _mark_reader_eof(self)
        return data, end_of_chunk

    return readchunk


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #
def _elapsed_ms(start: float) -> float:
    return round((time.perf_counter() - start) * 1000.0, 3)


def _format_error(exc: BaseException) -> str:
    return "{}: {}".format(type(exc).__name__, exc)


def _header(headers: Sequence, name: str) -> str:
    wanted = name.lower()
    for key, value in headers:
        if str(key).lower() == wanted:
            return str(value)
    return ""


def _redirect_url(status: int, headers: Sequence) -> str:
    if 300 <= int(status or 0) < 400:
        return _header(headers, "location")
    return ""


def _bodiless_response(status: int, method: str, headers: Sequence) -> bool:
    """响应按定义没有体（HEAD、204、304、Content-Length: 0）。"""
    return (
        str(method or "").upper() == "HEAD"
        or int(status) in (204, 304)
        or _header(headers, "content-length").strip() == "0"
    )


def _requests_http_version(response) -> str:
    version = getattr(getattr(response, "raw", None), "version", None)
    if version == 10:
        return "HTTP/1.0"
    if version == 20:
        return "HTTP/2"
    return "HTTP/1.1"


def _install_requests_stream_capture(response, collector: _BodyCollector) -> None:
    """在 ``requests`` 向调用方流式传输字节时收集这些字节。"""
    original = response.iter_content

    def iter_content(chunk_size=1, decode_unicode=False):
        if collector.started:
            for chunk in original(
                chunk_size=chunk_size, decode_unicode=decode_unicode
            ):
                yield chunk
            return
        collector.started = True
        for chunk in original(chunk_size=chunk_size, decode_unicode=decode_unicode):
            if isinstance(chunk, bytes):
                collector.append(chunk)
            elif isinstance(chunk, str):
                collector.append(
                    chunk.encode(response.encoding or "utf-8", "replace")
                )
            yield chunk
        collector.eof = True

    response.iter_content = iter_content


def _install_httpx_stream_capture(response, collector: _BodyCollector) -> None:
    """在 ``httpx`` 向调用方流式传输解码后的字节时收集这些字节。"""
    original = response.iter_bytes

    def iter_bytes(chunk_size=None):
        if collector.started:
            for chunk in original(chunk_size=chunk_size):
                yield chunk
            return
        collector.started = True
        for chunk in original(chunk_size=chunk_size):
            collector.append(chunk)
            yield chunk
        collector.eof = True

    response.iter_bytes = iter_bytes


def _install_httpx_async_stream_capture(
    response, collector: _BodyCollector
) -> None:
    original = response.aiter_bytes

    async def aiter_bytes(chunk_size=None):
        if collector.started:
            async for chunk in original(chunk_size=chunk_size):
                yield chunk
            return
        collector.started = True
        async for chunk in original(chunk_size=chunk_size):
            collector.append(chunk)
            yield chunk
        collector.eof = True

    response.aiter_bytes = aiter_bytes


# --------------------------------------------------------------------------- #
# requests
# --------------------------------------------------------------------------- #
@dataclass
class _Snapshot:
    data: Optional[bytes] = None
    size: Optional[int] = None
    source: str = ""
    unreadable: bool = False
    truncated: bool = False


def _cap_bytes(data: Optional[bytes], limit: int) -> _Snapshot:
    if data is None:
        return _Snapshot()
    data = bytes(data)
    if limit > 0 and len(data) > limit:
        return _Snapshot(data=data[:limit], size=len(data), truncated=True)
    return _Snapshot(data=data)


class _TeeReader:
    """在 requests 消费不可重定位的上传流时记录字节。"""

    __slots__ = ("_stream", "_collector")

    def __init__(self, stream, collector: _BodyCollector) -> None:
        self._stream = stream
        self._collector = collector

    def _record(self, data, requested=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        if data:
            self._collector.append(data)
        elif requested != 0:
            self._collector.eof = True
        return data

    def read(self, *args, **kwargs):
        requested = args[0] if args else kwargs.get("size", kwargs.get("amt"))
        data = self._stream.read(*args, **kwargs)
        self._record(data, requested)
        return data

    def readinto(self, buffer):
        readinto = getattr(self._stream, "readinto", None)
        if readinto is None:
            data = self.read(len(buffer))
            count = len(data)
            buffer[:count] = data
            return count
        count = readinto(buffer)
        if count:
            self._collector.append(bytes(memoryview(buffer)[:count]))
        elif count == 0:
            self._collector.eof = True
        return count

    def __len__(self):
        return len(self._stream)

    def __getattr__(self, name):
        return getattr(self._stream, name)


def _is_requests_stream_body(body) -> bool:
    if body is None or isinstance(body, (str, bytes, bytearray, memoryview)):
        return False
    if hasattr(body, "read"):
        return not _is_rewindable(body)
    return hasattr(body, "__iter__")


def _is_requests_too_many_redirects(exc: BaseException) -> bool:
    try:
        import requests
    except Exception:
        return False
    return isinstance(exc, requests.exceptions.TooManyRedirects)


def _requests_not_sent(exc: BaseException) -> bool:
    try:
        import requests
        from urllib3.exceptions import ProtocolError
    except Exception:
        return False
    if not isinstance(exc, requests.exceptions.ConnectionError):
        return False
    cause = exc.args[0] if exc.args else None
    # ProtocolError/OSError 也可能发生在请求发出之后（例如读取响应时连接
    # 被重置），这类失败不能标记为未发送。ConnectionError 的其余来源
    # （连接被拒绝、连接超时、代理或 TLS 握手失败）都发生在建立连接阶段。
    return not isinstance(cause, (ProtocolError, OSError))


def _tee_iterable(iterable, collector: _BodyCollector):
    for chunk in iterable:
        if isinstance(chunk, bytes):
            collector.append(chunk)
        elif isinstance(chunk, str):
            collector.append(chunk.encode("utf-8"))
        yield chunk
    collector.eof = True


def _snapshot_from_collector(
    collector: _BodyCollector, expected: Optional[int] = None
) -> _Snapshot:
    complete = collector.eof or (
        expected is not None and collector.total >= expected
    )
    if complete:
        collector.eof = True
        data = _collector_body(collector) or b""
        return _Snapshot(
            data=data,
            size=collector.total if collector.truncated else None,
            truncated=collector.truncated,
        )
    data = b"".join(collector.chunks)
    if not data:
        return _Snapshot(unreadable=True)
    return _Snapshot(data=data, truncated=True)


def _requests_body_snapshot(body, limit: int = 0) -> _Snapshot:
    """在不消费可重定位文件的情况下描述 requests 的请求体。"""
    if isinstance(body, str):
        return _cap_bytes(body.encode("utf-8"), limit)
    if isinstance(body, (bytes, bytearray)):
        return _cap_bytes(bytes(body), limit)
    if body is None:
        return _Snapshot()
    # 在不消费可重定位文件的情况下对其做快照。生成器和
    # 不可重定位的流无法回放，并会如实上报。
    return _snapshot_file(body, limit=limit)


def _request_from_requests(prepared, snapshot=None) -> RequestRecord:
    headers = [(str(key), str(value)) for key, value in prepared.headers.items()]
    if snapshot is None:
        snapshot = _requests_body_snapshot(
            getattr(prepared, "body", None), _body_limit()
        )
    return RequestRecord(
        method=prepared.method or "GET",
        url=prepared.url,
        headers=headers,
        body=snapshot.data,
        unreadable=snapshot.unreadable,
        body_size=snapshot.size,
        body_source=snapshot.source,
        truncated=snapshot.truncated,
        full_body_size=snapshot.size if snapshot.truncated else None,
    )


def _response_headers_from_requests(response) -> List[Tuple[str, str]]:
    """优先使用 urllib3 的头字典，以保留重复的头（Set-Cookie）。"""
    raw_headers = getattr(getattr(response, "raw", None), "headers", None)
    if raw_headers is not None:
        try:
            pairs = [(str(key), str(value)) for key, value in raw_headers.items()]
        except Exception:
            pairs = []
        if pairs:
            return pairs
    return [(str(key), str(value)) for key, value in response.headers.items()]


def _response_from_requests(response, streaming: bool, collector=None) -> ResponseRecord:
    body = None
    is_streaming = False
    provider = None
    if streaming:
        if collector is not None:
            provider = _body_provider(collector)
        else:
            is_streaming = True
    else:
        try:
            body = response.content
        except Exception:
            is_streaming = True
    headers = _response_headers_from_requests(response)
    if _bodiless_response(
        response.status_code,
        getattr(getattr(response, "request", None), "method", ""),
        headers,
    ):
        # 无体响应即使走 stream=True 也没有可收集的字节；必须报告 0，
        # 而不是把未读取的体当作未知大小。
        body = b""
        is_streaming = False
        provider = None
    truncated = False
    full_body_size = None
    if body is not None:
        snapshot = _cap_bytes(body, _body_limit())
        body = snapshot.data
        truncated = snapshot.truncated
        full_body_size = snapshot.size if snapshot.truncated else None
    return ResponseRecord(
        status=response.status_code,
        status_text=response.reason or "",
        headers=headers,
        body=body,
        mime_type=_header(headers, "content-type"),
        http_version=_requests_http_version(response),
        streaming=is_streaming,
        redirect_url=_redirect_url(response.status_code, headers),
        body_provider=provider,
        truncated=truncated,
        full_body_size=full_body_size,
    )


def _make_requests_send(original):
    def send(self, request, **kwargs):
        recorder = _active()
        if recorder is None or not recorder.is_capturing():
            return original(self, request, **kwargs)
        if _depth.get() and not kwargs.get("allow_redirects", True):
            # requests 通过以 allow_redirects=False 再次调用 Session.send 来
            # 解析重定向；外层调用记录整条重定向链。
            return original(self, request, **kwargs)

        token = _depth.set(1)
        try:
            started_at = datetime.now(timezone.utc)
            start = time.perf_counter()
            # requests 在发送时会消费类文件的体，因此在请求运行前
            # 先对其做快照，并复用这些字节用于回放。
            body_obj = getattr(request, "body", None)
            tee_collector = None
            tee_expected = None
            if _is_requests_stream_body(body_obj):
                tee_collector = _BodyCollector(_body_limit())
                try:
                    tee_expected = len(body_obj)
                except Exception:
                    tee_expected = None
                if hasattr(body_obj, "read"):
                    request.body = _TeeReader(body_obj, tee_collector)
                else:
                    request.body = _tee_iterable(body_obj, tee_collector)
                snapshot = _Snapshot()
                original_body = request.body
            else:
                snapshot = _requests_body_snapshot(body_obj, _body_limit())
                original_body = body_obj

            def hop_snapshot(hop_request):
                # 307/308 各跳保留同一个体对象；在首次发送后重新对其
                # 做快照会从 EOF 处读取文件，从而记录到
                # 空的体。
                if getattr(hop_request, "body", None) is original_body:
                    return snapshot
                return None

            try:
                response = original(self, request, **kwargs)
            except BaseException as exc:
                if tee_collector is not None:
                    snapshot = _snapshot_from_collector(
                        tee_collector, tee_expected
                    )
                failed_response = getattr(exc, "response", None)
                truncated = _is_requests_too_many_redirects(exc)
                hops = (
                    list(getattr(failed_response, "history", None) or [])
                    + [failed_response]
                    if failed_response is not None
                    else []
                )
                if hops:
                    cumulative_ms = 0.0
                    for index, hop in enumerate(hops):
                        is_last = index == len(hops) - 1
                        request_record = _request_from_requests(
                            hop.request,
                            snapshot=hop_snapshot(hop.request),
                        )
                        request_record.http_version = _requests_http_version(hop)
                        hop_elapsed = getattr(hop, "elapsed", None)
                        hop_ms = (
                            round(hop_elapsed.total_seconds() * 1000.0, 3)
                            if hop_elapsed is not None
                            else 0.0
                        )
                        hop_start = started_at + timedelta(milliseconds=cumulative_ms)
                        cumulative_ms += hop_ms
                        recorder.record(
                            Exchange(
                                request=request_record,
                                started_at=hop_start,
                                elapsed_ms=hop_ms,
                                response=_response_from_requests(hop, streaming=True),
                                error=_format_error(exc) if is_last else None,
                                redirects_truncated=truncated and is_last,
                            )
                        )
                else:
                    request_record = _request_from_requests(
                        request, snapshot=snapshot
                    )
                    request_record.not_sent = _requests_not_sent(exc)
                    recorder.record(
                        Exchange(
                            request=request_record,
                            started_at=started_at,
                            elapsed_ms=_elapsed_ms(start),
                            error=_format_error(exc),
                        )
                    )
                raise

            if tee_collector is not None:
                snapshot = _snapshot_from_collector(tee_collector, tee_expected)
            stream = bool(kwargs.get("stream", False))
            collector = None
            if stream:
                collector = _BodyCollector(_body_limit())
                _install_requests_stream_capture(response, collector)
            hops = list(getattr(response, "history", None) or []) + [response]
            cumulative_ms = 0.0
            for hop in hops:
                request_record = _request_from_requests(
                    hop.request,
                    snapshot=hop_snapshot(hop.request),
                )
                request_record.http_version = _requests_http_version(hop)
                hop_elapsed = getattr(hop, "elapsed", None)
                hop_ms = (
                    round(hop_elapsed.total_seconds() * 1000.0, 3)
                    if hop_elapsed is not None
                    else 0.0
                )
                hop_start = started_at + timedelta(milliseconds=cumulative_ms)
                cumulative_ms += hop_ms
                recorder.record(
                    Exchange(
                        request=request_record,
                        started_at=hop_start,
                        elapsed_ms=hop_ms,
                        response=_response_from_requests(
                            hop,
                            stream and hop is response,
                            collector if hop is response else None,
                        ),
                    )
                )
            return response
        finally:
            _depth.reset(token)

    return send


# --------------------------------------------------------------------------- #
# httpx
# --------------------------------------------------------------------------- #
def _httpx_request_body(request) -> Tuple[Optional[bytes], bool]:
    """返回 ``(body, unreadable)``，不根据空的体进行猜测。"""
    try:
        content = request.content
    except Exception:
        headers = request.headers
        if not headers.get("content-length") and not headers.get(
            "transfer-encoding"
        ):
            return None, False
        return None, True
    if content:
        return bytes(content), False
    return None, False


def _httpx_content_length(request) -> Optional[int]:
    try:
        raw = request.headers.get("content-length")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


_TEE_SYNC_CLASS = None
_TEE_ASYNC_CLASS = None


def _tee_sync_stream(stream, collector: _BodyCollector):
    """包装同步请求流：首次迭代收集字节，后续迭代原样透传。"""
    global _TEE_SYNC_CLASS
    if _TEE_SYNC_CLASS is None:
        from httpx import SyncByteStream

        class _TeeSyncStream(SyncByteStream):
            def __init__(self, inner, body_collector) -> None:
                self._inner = inner
                self._collector = body_collector
                self._started = False

            def __iter__(self):
                first = not self._started
                self._started = True
                if not first:
                    yield from self._inner
                    return
                for chunk in self._inner:
                    self._collector.append(chunk)
                    yield chunk
                self._collector.eof = True

        _TEE_SYNC_CLASS = _TeeSyncStream
    return _TEE_SYNC_CLASS(stream, collector)


def _tee_async_stream(stream, collector: _BodyCollector):
    """包装异步请求流：首次迭代收集字节，后续迭代原样透传。"""
    global _TEE_ASYNC_CLASS
    if _TEE_ASYNC_CLASS is None:
        from httpx import AsyncByteStream

        class _TeeAsyncStream(AsyncByteStream):
            def __init__(self, inner, body_collector) -> None:
                self._inner = inner
                self._collector = body_collector
                self._started = False

            async def __aiter__(self):
                first = not self._started
                self._started = True
                if not first:
                    async for chunk in self._inner:
                        yield chunk
                    return
                async for chunk in self._inner:
                    self._collector.append(chunk)
                    yield chunk
                self._collector.eof = True

        _TEE_ASYNC_CLASS = _TeeAsyncStream
    return _TEE_ASYNC_CLASS(stream, collector)


def _prepare_httpx_body(request, is_async: bool):
    """返回 ``(snapshot, collector, expected, original_stream, wrapped_stream)``。

    可读的体直接快照；流式/文件上传改用 tee，避免把整个上传读入内存，
    同时保持 httpx 原有的流式与重放语义。
    """
    body, unreadable = _httpx_request_body(request)
    if body is not None:
        return _cap_bytes(body, _body_limit()), None, None, None, None
    if not unreadable:
        return _Snapshot(), None, None, None, None

    stream = getattr(request, "stream", None)
    if stream is None:
        return _Snapshot(unreadable=True), None, None, None, None
    collector = _BodyCollector(_body_limit())
    expected = _httpx_content_length(request)
    if is_async:
        if not hasattr(stream, "__aiter__"):
            return _Snapshot(unreadable=True), None, None, None, None
        wrapped = _tee_async_stream(stream, collector)
    else:
        if not hasattr(stream, "__iter__"):
            return _Snapshot(unreadable=True), None, None, None, None
        wrapped = _tee_sync_stream(stream, collector)
    request.stream = wrapped
    return _Snapshot(), collector, expected, stream, wrapped


def _request_from_httpx(
    request, snapshot: Optional[_Snapshot] = None
) -> RequestRecord:
    try:
        headers = [
            (str(key), str(value)) for key, value in request.headers.multi_items()
        ]
    except Exception:
        headers = [(str(key), str(value)) for key, value in request.headers.items()]
    if snapshot is None:
        body, unreadable = _httpx_request_body(request)
        if body is not None:
            snapshot = _cap_bytes(body, _body_limit())
        else:
            snapshot = _Snapshot(unreadable=unreadable)
    return RequestRecord(
        method=request.method,
        url=str(request.url),
        headers=headers,
        body=snapshot.data,
        unreadable=snapshot.unreadable,
        body_size=snapshot.size,
        truncated=snapshot.truncated,
        full_body_size=snapshot.size if snapshot.truncated else None,
    )


def _httpx_request_record(
    hop_request,
    index: int,
    first_snapshot: _Snapshot,
    http_version: Optional[str] = None,
) -> RequestRecord:
    if index == 0:
        request = _request_from_httpx(hop_request, snapshot=first_snapshot)
    else:
        method = str(hop_request.method).upper()
        if method in ("GET", "HEAD") or (
            first_snapshot.data is None and not first_snapshot.unreadable
        ):
            request = _request_from_httpx(hop_request, snapshot=_Snapshot())
        elif first_snapshot.data is not None:
            # 307/308 保留原始方法和载荷。
            request = _request_from_httpx(hop_request, snapshot=first_snapshot)
        else:
            request = _request_from_httpx(
                hop_request, snapshot=_Snapshot(unreadable=True)
            )
    request.http_version = http_version or "HTTP/1.1"
    return request


def _httpx_hop_request(
    hop, index: int, first_snapshot: _Snapshot
) -> RequestRecord:
    return _httpx_request_record(
        hop.request,
        index,
        first_snapshot,
        getattr(hop, "http_version", "HTTP/1.1") or "HTTP/1.1",
    )


def _is_httpx_too_many_redirects(exc: BaseException) -> bool:
    try:
        import httpx
    except Exception:
        return False
    return isinstance(exc, httpx.TooManyRedirects)


def _httpx_not_sent(exc: BaseException) -> bool:
    try:
        import httpx
    except Exception:
        return False
    return isinstance(
        exc, (httpx.ConnectError, httpx.ConnectTimeout, httpx.StreamConsumed)
    )


def _record_httpx_error(
    recorder,
    hops,
    request,
    first_snapshot: _Snapshot,
    started_at: datetime,
    elapsed_ms: float,
    exc: BaseException,
) -> None:
    error = _format_error(exc)
    truncated = _is_httpx_too_many_redirects(exc)
    not_sent = _httpx_not_sent(exc)
    if not hops:
        request_record = _request_from_httpx(
            request, snapshot=first_snapshot
        )
        if truncated or not_sent:
            # httpx 抛出 TooManyRedirects 时携带的是下一个请求，而它从未
            # 发送；连接阶段的失败同样没有到达传输层。消费方不得把这些
            # 请求误当作线上实际传输的流量。
            request_record.not_sent = True
        recorder.record(
            Exchange(
                request=request_record,
                started_at=started_at,
                elapsed_ms=elapsed_ms,
                error=error,
                redirects_truncated=truncated,
            )
        )
        return

    for index, (hop_request, hop_response) in enumerate(hops):
        is_last = index == len(hops) - 1
        request_record = _httpx_request_record(
            hop_request,
            index,
            first_snapshot,
            (
                getattr(hop_response, "http_version", "HTTP/1.1")
                if hop_response is not None
                else "HTTP/1.1"
            ),
        )
        if is_last and hop_response is None and not_sent:
            request_record.not_sent = True
        response_record = (
            _response_from_httpx(hop_response, streaming=False)
            if hop_response is not None
            else None
        )
        recorder.record(
            Exchange(
                request=request_record,
                started_at=started_at,
                elapsed_ms=elapsed_ms if is_last else 0.0,
                response=response_record,
                error=error if is_last else None,
                redirects_truncated=truncated and is_last,
            )
        )


def _response_from_httpx(response, streaming: bool, collector=None) -> ResponseRecord:
    body = None
    is_streaming = False
    provider = None
    if streaming:
        if collector is not None:
            provider = _body_provider(collector)
        else:
            is_streaming = True
    else:
        try:
            body = response.content
        except Exception:
            is_streaming = True
    try:
        headers = [
            (str(key), str(value)) for key, value in response.headers.multi_items()
        ]
    except Exception:
        headers = [(str(key), str(value)) for key, value in response.headers.items()]
    status = response.status_code
    if _bodiless_response(
        status,
        getattr(getattr(response, "request", None), "method", ""),
        headers,
    ):
        body = b""
        is_streaming = False
        provider = None
    truncated = False
    full_body_size = None
    if body is not None:
        snapshot = _cap_bytes(body, _body_limit())
        body = snapshot.data
        truncated = snapshot.truncated
        full_body_size = snapshot.size if snapshot.truncated else None
    return ResponseRecord(
        status=status,
        status_text=response.reason_phrase or "",
        headers=headers,
        body=body,
        mime_type=_header(headers, "content-type"),
        http_version=getattr(response, "http_version", "HTTP/1.1") or "HTTP/1.1",
        streaming=is_streaming,
        redirect_url=_redirect_url(status, headers),
        body_provider=provider,
        truncated=truncated,
        full_body_size=full_body_size,
    )


def _make_httpx_single_request(original):
    def _send_single_request(self, request):
        hops = _httpx_hops.get()
        entry = None
        if hops is not None:
            entry = [request, None]
            hops.append(entry)
        response = original(self, request)
        if entry is not None:
            entry[1] = response
        return response

    return _send_single_request


def _make_httpx_async_single_request(original):
    async def _send_single_request(self, request):
        hops = _httpx_hops.get()
        entry = None
        if hops is not None:
            entry = [request, None]
            hops.append(entry)
        response = await original(self, request)
        if entry is not None:
            entry[1] = response
        return response

    return _send_single_request


def _make_httpx_send(original):
    def send(self, request, **kwargs):
        recorder = _active()
        if recorder is None or not recorder.is_capturing():
            return original(self, request, **kwargs)

        first_snapshot, body_collector, expected, original_stream, wrapped = (
            _prepare_httpx_body(request, is_async=False)
        )

        started_at = datetime.now(timezone.utc)
        start = time.perf_counter()
        token = _httpx_hops.set([])
        try:
            response = original(self, request, **kwargs)
        except BaseException as exc:
            if body_collector is not None:
                first_snapshot = _snapshot_from_collector(
                    body_collector, expected
                )
            _record_httpx_error(
                recorder,
                _httpx_hops.get() or [],
                request,
                first_snapshot,
                started_at,
                _elapsed_ms(start),
                exc,
            )
            raise
        finally:
            _httpx_hops.reset(token)
            if original_stream is not None and request.stream is wrapped:
                request.stream = original_stream

        if body_collector is not None:
            first_snapshot = _snapshot_from_collector(body_collector, expected)

        elapsed = _elapsed_ms(start)
        stream = bool(kwargs.get("stream", False))
        response_collector = None
        if stream:
            response_collector = _BodyCollector(_body_limit())
            _install_httpx_stream_capture(response, response_collector)
        hops = list(getattr(response, "history", None) or []) + [response]
        for index, hop in enumerate(hops):
            recorder.record(
                Exchange(
                    request=_httpx_hop_request(hop, index, first_snapshot),
                    started_at=started_at,
                    # httpx 不暴露每跳的计时；只让最终响应获得测得的耗时，
                    # 而不是用整条重定向链的时间去夸大
                    # 每一跳。
                    elapsed_ms=elapsed if hop is response else 0.0,
                    response=_response_from_httpx(
                        hop,
                        stream and hop is response,
                        response_collector if hop is response else None,
                    ),
                )
            )
        return response

    return send


def _make_httpx_async_send(original):
    async def send(self, request, **kwargs):
        recorder = _active()
        if recorder is None or not recorder.is_capturing():
            return await original(self, request, **kwargs)

        first_snapshot, body_collector, expected, original_stream, wrapped = (
            _prepare_httpx_body(request, is_async=True)
        )

        started_at = datetime.now(timezone.utc)
        start = time.perf_counter()
        token = _httpx_hops.set([])
        try:
            response = await original(self, request, **kwargs)
        except BaseException as exc:
            if body_collector is not None:
                first_snapshot = _snapshot_from_collector(
                    body_collector, expected
                )
            _record_httpx_error(
                recorder,
                _httpx_hops.get() or [],
                request,
                first_snapshot,
                started_at,
                _elapsed_ms(start),
                exc,
            )
            raise
        finally:
            _httpx_hops.reset(token)
            if original_stream is not None and request.stream is wrapped:
                request.stream = original_stream

        if body_collector is not None:
            first_snapshot = _snapshot_from_collector(body_collector, expected)

        elapsed = _elapsed_ms(start)
        stream = bool(kwargs.get("stream", False))
        response_collector = None
        if stream:
            response_collector = _BodyCollector(_body_limit())
            _install_httpx_async_stream_capture(response, response_collector)
        hops = list(getattr(response, "history", None) or []) + [response]
        for index, hop in enumerate(hops):
            recorder.record(
                Exchange(
                    request=_httpx_hop_request(hop, index, first_snapshot),
                    started_at=started_at,
                    # httpx 不暴露每跳的计时；只让最终响应获得测得的耗时，
                    # 而不是用整条重定向链的时间去夸大
                    # 每一跳。
                    elapsed_ms=elapsed if hop is response else 0.0,
                    response=_response_from_httpx(
                        hop,
                        stream and hop is response,
                        response_collector if hop is response else None,
                    ),
                )
            )
        return response

    return send


# --------------------------------------------------------------------------- #
# aiohttp
# --------------------------------------------------------------------------- #
def _multidict_types():
    try:
        from multidict import MultiDict, MultiDictProxy

        return (MultiDict, MultiDictProxy)
    except ImportError:
        return ()


@dataclass
class _AiohttpPayload:
    body: Optional[bytes] = None
    unreadable: bool = False
    truncated: bool = False
    parts: Optional[List[Part]] = None
    size: Optional[int] = None
    source: str = ""


def _file_metadata(value) -> Tuple[Optional[int], str]:
    size = None
    try:
        size = os.fstat(value.fileno()).st_size
    except Exception:
        pass
    source = str(getattr(value, "name", "") or "")
    return size, source


def _is_rewindable(value) -> bool:
    """仅对能够安全还原其位置的流返回 True。

    管道、套接字和 tty 都不是常规文件。在 Windows 上，管道甚至
    会报告 ``seekable() == True`` 并接受 ``seek()`` 却并不回绕，
    因此读取它会悄无声息地消费掉上传内容。
    """
    fileno = getattr(value, "fileno", None)
    if callable(fileno):
        try:
            return stat.S_ISREG(os.fstat(fileno()).st_mode)
        except Exception:
            pass
    seekable = getattr(value, "seekable", None)
    if callable(seekable):
        try:
            return bool(seekable())
        except Exception:
            return False
    return False


def _snapshot_file(
    value, encoding: str = "utf-8", limit: int = 0
) -> _Snapshot:
    """读取可回绕的文件对象并还原其位置。"""
    read = getattr(value, "read", None)
    tell = getattr(value, "tell", None)
    seek = getattr(value, "seek", None)
    if not (callable(read) and callable(tell) and callable(seek)):
        size, source = _file_metadata(value)
        return _Snapshot(size=size, source=source, unreadable=True)
    if not _is_rewindable(value):
        size, source = _file_metadata(value)
        return _Snapshot(size=size, source=source, unreadable=True)
    position = None
    try:
        position = tell()
        full_size = None
        try:
            full_size = os.fstat(value.fileno()).st_size - int(position)
        except Exception:
            full_size = None
        if full_size is not None and full_size < 0:
            full_size = None
        # 文本模式的 ``tell()`` 可能返回不透明 cookie 而非字节偏移，
        # 因此不对文本流使用 ``fstat`` 得到的大小。
        binary = "b" in str(getattr(value, "mode", "b") or "b")
        if limit > 0:
            # 始终有界读取：即使拿不到文件大小（例如管道包装或异常流），
            # 也不能把整个文件读进内存。文本模式读取 ``limit + 1`` 个字符，
            # 其编码后的字节数不会少于字符数。
            data = read(limit + 1)
            if isinstance(data, str):
                data = data.encode(encoding)
            data = bytes(data)
            if len(data) > limit:
                return _Snapshot(
                    data=data[:limit],
                    size=full_size if binary else None,
                    truncated=True,
                )
            return _Snapshot(data=data)
        data = read()
        if isinstance(data, str):
            data = data.encode(encoding)
        return _Snapshot(data=bytes(data))
    except Exception:
        pass
    finally:
        if position is not None:
            try:
                seek(position)
            except Exception:
                pass
    size, source = _file_metadata(value)
    return _Snapshot(size=size, source=source, unreadable=True)


def _aiohttp_part_content_type(value, charset: str) -> str:
    """照搬 aiohttp 的载荷类所选择的默认 Content-Type。"""
    if isinstance(value, (str, int, float, io.TextIOBase)):
        return "text/plain; charset={}".format(charset)
    return "application/octet-stream"


def _aiohttp_generated_body(data: bytes) -> _AiohttpPayload:
    """按捕获上限描述插件自身生成的请求体。"""
    snapshot = _cap_bytes(data, _body_limit())
    return _AiohttpPayload(
        body=snapshot.data,
        truncated=snapshot.truncated,
        size=snapshot.size,
    )


def _aiohttp_formdata_payload(data) -> _AiohttpPayload:
    """在不消费文件对象的情况下描述 aiohttp 的 ``FormData``。"""
    try:
        fields = list(data._fields)
    except Exception:
        return _AiohttpPayload(unreadable=True)
    if not fields:
        return _AiohttpPayload(unreadable=True)
    charset = getattr(data, "_charset", None) or "utf-8"
    if getattr(data, "is_multipart", False):
        parts: List[Part] = []
        for type_options, part_headers, value in fields:
            name = type_options.get("name", "") if type_options else ""
            filename = type_options.get("filename") if type_options else None
            content_type = None
            if part_headers:
                content_type = part_headers.get("Content-Type") or part_headers.get(
                    "content-type"
                )
            if content_type is None:
                content_type = _aiohttp_part_content_type(value, charset)
            part_truncated = False
            part_size = None
            if hasattr(value, "read"):
                snapshot = _snapshot_file(value, charset, _body_limit())
                if snapshot.unreadable:
                    parts.append(
                        Part(
                            name=str(name),
                            value=b"",
                            filename=(
                                str(filename) if filename is not None else None
                            ),
                            content_type=content_type,
                            unreadable=True,
                            size=snapshot.size,
                            source=snapshot.source,
                        )
                    )
                    continue
                body = snapshot.data
                part_truncated = snapshot.truncated
                part_size = snapshot.size if snapshot.truncated else None
            elif isinstance(value, (str, int, float)):
                snapshot = _cap_bytes(str(value).encode(charset), _body_limit())
                body = snapshot.data
                part_truncated = snapshot.truncated
                part_size = snapshot.size if snapshot.truncated else None
            elif isinstance(value, (bytes, bytearray, memoryview)):
                snapshot = _cap_bytes(bytes(value), _body_limit())
                body = snapshot.data
                part_truncated = snapshot.truncated
                part_size = snapshot.size if snapshot.truncated else None
            else:
                size, source = _file_metadata(value)
                return _AiohttpPayload(unreadable=True, size=size, source=source)
            parts.append(
                Part(
                    name=str(name),
                    value=body,
                    filename=str(filename) if filename is not None else None,
                    content_type=content_type,
                    truncated=part_truncated,
                    size=part_size,
                )
            )
        return _AiohttpPayload(
            parts=parts or None,
            truncated=any(part.truncated for part in parts),
        )

    pairs = []
    for type_options, _, value in fields:
        if hasattr(value, "read"):
            size, source = _file_metadata(value)
            return _AiohttpPayload(unreadable=True, size=size, source=source)
        if not isinstance(value, (str, int, float)):
            return _AiohttpPayload(unreadable=True)
        pairs.append((type_options.get("name", ""), value))
    try:
        return _aiohttp_generated_body(
            urlencode(pairs, doseq=True, encoding=charset).encode()
        )
    except Exception:
        return _AiohttpPayload(unreadable=True)


def _aiohttp_complex_value(value) -> bool:
    """对 aiohttp 会转换为 multipart 文件字段的值返回 True。"""
    return isinstance(value, (bytes, bytearray, memoryview)) or hasattr(
        value, "read"
    )


def _aiohttp_formdata_from(data) -> _AiohttpPayload:
    """照搬 aiohttp 自身的 ``FormData(mapping_or_pairs)`` 转换。"""
    try:
        from aiohttp import FormData
    except Exception:
        return _AiohttpPayload(unreadable=True)
    try:
        with warnings.catch_warnings():
            # aiohttp 3.14 警告 bytes 值在 4.0 中将不再变为文件字段；
            # 捕获我们实际看到的 3.x 线上格式。
            warnings.simplefilter("ignore", DeprecationWarning)
            form = FormData(data)
    except Exception:
        return _AiohttpPayload(unreadable=True)
    return _aiohttp_formdata_payload(form)


def _aiohttp_payload(data) -> _AiohttpPayload:
    """在可能的情况下，不消费 aiohttp 的 ``data=`` 值即对其进行描述。"""
    if data is None:
        return _AiohttpPayload()

    try:
        from aiohttp import FormData
    except Exception:
        FormData = None

    if FormData is not None and isinstance(data, FormData):
        return _aiohttp_formdata_payload(data)

    if isinstance(data, dict) or (isinstance(data, _multidict_types())):
        try:
            pairs = list(data.items())
        except Exception:
            return _AiohttpPayload(unreadable=True)
        if any(_aiohttp_complex_value(value) for _, value in pairs):
            return _aiohttp_formdata_from(data)
        for _, value in pairs:
            if not isinstance(value, (str, int, float)):
                return _AiohttpPayload(unreadable=True)
        try:
            return _aiohttp_generated_body(
                urlencode(pairs, doseq=True).encode("utf-8")
            )
        except Exception:
            return _AiohttpPayload(unreadable=True)

    if isinstance(data, (list, tuple)):
        pairs = []
        try:
            for item in data:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    return _AiohttpPayload(unreadable=True)
                pairs.append((item[0], item[1]))
        except Exception:
            return _AiohttpPayload(unreadable=True)
        if any(_aiohttp_complex_value(value) for _, value in pairs):
            return _aiohttp_formdata_from(data)
        for _, value in pairs:
            if not isinstance(value, (str, int, float)):
                return _AiohttpPayload(unreadable=True)
        try:
            return _aiohttp_generated_body(
                urlencode(pairs, doseq=True).encode("utf-8")
            )
        except Exception:
            return _AiohttpPayload(unreadable=True)

    if isinstance(data, str):
        return _aiohttp_generated_body(data.encode("utf-8"))
    if isinstance(data, (bytes, bytearray, memoryview)):
        return _aiohttp_generated_body(bytes(data))
    if hasattr(data, "read"):
        snapshot = _snapshot_file(data, limit=_body_limit())
        return _AiohttpPayload(
            body=snapshot.data,
            unreadable=snapshot.unreadable,
            truncated=snapshot.truncated,
            size=snapshot.size,
            source=snapshot.source,
        )
    return _AiohttpPayload(unreadable=True)


def _aiohttp_body_from_kwargs(kwargs) -> _AiohttpPayload:
    if kwargs.get("json") is not None:
        try:
            # 与 aiohttp 自身的序列化保持一致（json.dumps 默认值）。
            return _aiohttp_generated_body(
                json.dumps(kwargs["json"]).encode("utf-8")
            )
        except (TypeError, ValueError):
            return _AiohttpPayload(unreadable=True)
    return _aiohttp_payload(kwargs.get("data"))


def _aiohttp_http_version(version) -> str:
    try:
        return "HTTP/{}.{}".format(version.major, version.minor)
    except Exception:
        return "HTTP/1.1"


def _request_from_aiohttp_info(
    info,
    body: Optional[bytes],
    unreadable: bool,
    parts: Optional[List[Part]],
    body_size: Optional[int] = None,
    body_source: str = "",
    truncated: bool = False,
) -> RequestRecord:
    headers = [(str(key), str(value)) for key, value in info.headers.items()]
    return RequestRecord(
        method=info.method,
        url=str(info.url),
        headers=headers,
        body=body,
        http_version=_aiohttp_http_version(getattr(info, "version", None)),
        unreadable=unreadable,
        body_size=body_size,
        body_source=body_source,
        parts=parts,
        truncated=truncated,
        full_body_size=body_size if truncated else None,
    )


def _aiohttp_hop_request(hop, index: int, payload: _AiohttpPayload) -> RequestRecord:
    info = hop.request_info
    if index == 0:
        return _request_from_aiohttp_info(
            info,
            payload.body,
            payload.unreadable,
            payload.parts,
            payload.size,
            payload.source,
            payload.truncated,
        )
    method = str(info.method).upper()
    if method in ("GET", "HEAD") or (
        payload.body is None and payload.parts is None and not payload.unreadable
    ):
        return _request_from_aiohttp_info(info, None, False, None)
    if payload.parts is not None:
        return _request_from_aiohttp_info(
            info,
            None,
            False,
            payload.parts,
            truncated=any(part.truncated for part in payload.parts),
        )
    if payload.body is not None:
        # 307/308 保留原始方法和载荷。
        return _request_from_aiohttp_info(
            info,
            payload.body,
            False,
            None,
            truncated=payload.truncated,
        )
    return _request_from_aiohttp_info(
        info, None, True, None, payload.size, payload.source
    )


def _header_present(headers, name: str) -> bool:
    wanted = name.lower()
    return any(str(key).lower() == wanted for key in headers)


def _aiohttp_default_headers() -> List[Tuple[str, str]]:
    try:
        from aiohttp.client_reqrep import ClientRequest

        defaults = ClientRequest.DEFAULT_HEADERS
    except Exception:
        return []
    try:
        return [(str(key), str(value)) for key, value in defaults.items()]
    except Exception:
        return []


def _aiohttp_default_user_agent() -> str:
    try:
        from aiohttp.http import SERVER_SOFTWARE

        return str(SERVER_SOFTWARE)
    except Exception:
        return ""


def _aiohttp_error_url_object(session, method: str, url, kwargs):
    try:
        from yarl import URL

        try:
            # 与 aiohttp 自身一致：相对 URL 需要用会话的 base_url 展开。
            target = session._build_url(str(url))
        except Exception:
            target = URL(str(url))
        params = kwargs.get("params")
        if params:
            target = target.update_query(params)
        return target
    except Exception:
        return None


def _aiohttp_error_url(session, method: str, url, kwargs) -> str:
    target = _aiohttp_error_url_object(session, method, url, kwargs)
    return str(target) if target is not None else str(url)


async def _aiohttp_cookie_header(session, url, kwargs) -> str:
    pairs = []
    seen = set()

    def add(name, value) -> None:
        name = str(name)
        if name in seen:
            return
        seen.add(name)
        pairs.append((name, str(value)))

    if url is not None:
        jar = getattr(session, "_cookie_jar", None)
        if jar is not None:
            try:
                filtered = jar.filter_cookies(url)
                if inspect.isawaitable(filtered):
                    filtered = await filtered
                for name, morsel in filtered.items():
                    add(name, getattr(morsel, "value", morsel))
            except Exception:
                pass

    cookies = kwargs.get("cookies")
    if isinstance(cookies, str):
        return cookies
    if cookies is not None:
        try:
            for name, value in cookies.items():
                add(name, value)
        except Exception:
            pass
    return "; ".join("{}={}".format(name, value) for name, value in pairs)


async def _aiohttp_error_headers(
    session, method: str, url, kwargs
) -> List[Tuple[str, str]]:
    merged = {}
    for key, value in _aiohttp_default_headers():
        merged.setdefault(key, value)
    for source in (getattr(session, "_default_headers", None), kwargs.get("headers")):
        try:
            for key, value in (source or {}).items():
                merged[str(key)] = str(value)
        except Exception:
            continue

    target = _aiohttp_error_url_object(session, method, url, kwargs)
    if target is not None:
        host = getattr(target, "host_port_subcomponent", None)
        if host and not _header_present(merged, "Host"):
            merged["Host"] = str(host)
    user_agent = _aiohttp_default_user_agent()
    if user_agent and not _header_present(merged, "User-Agent"):
        merged["User-Agent"] = user_agent
    if kwargs.get("json") is not None and not _header_present(
        merged, "Content-Type"
    ):
        merged["Content-Type"] = "application/json"
    cookie = await _aiohttp_cookie_header(session, target, kwargs)
    if cookie and not _header_present(merged, "Cookie"):
        merged["Cookie"] = cookie
    return list(merged.items())


def _aiohttp_not_sent(exc: BaseException) -> bool:
    try:
        import aiohttp
    except Exception:
        return False
    types = []
    for name in ("ClientConnectorError", "ConnectionTimeoutError"):
        candidate = getattr(aiohttp, name, None)
        if isinstance(candidate, type):
            types.append(candidate)
    return bool(types) and isinstance(exc, tuple(types))


async def _request_from_aiohttp_kwargs(
    session,
    method: str,
    url,
    kwargs,
    payload: _AiohttpPayload,
    exc: Optional[BaseException] = None,
) -> RequestRecord:
    return RequestRecord(
        method=str(method).upper(),
        url=_aiohttp_error_url(session, method, url, kwargs),
        headers=await _aiohttp_error_headers(session, method, url, kwargs),
        body=payload.body,
        unreadable=payload.unreadable,
        body_size=payload.size,
        body_source=payload.source,
        not_sent=_aiohttp_not_sent(exc) if exc is not None else False,
        headers_synthesized=True,
        parts=payload.parts,
        truncated=payload.truncated,
        full_body_size=payload.size if payload.truncated else None,
    )


def _aiohttp_body_provider(response, collector: _BodyCollector):
    expected: Optional[int] = None
    try:
        raw_length = response.headers.get("Content-Length")
        if raw_length is not None:
            expected = int(raw_length)
    except (TypeError, ValueError):
        expected = None
    content_encoding = str(response.headers.get("Content-Encoding", "")).strip().lower()
    method = str(getattr(getattr(response, "request_info", None), "method", "")).upper()

    def provide() -> Optional[bytes]:
        provide.truncated = collector.truncated
        provide.full_body_size = None
        if method == "HEAD" or response.status in (204, 304):
            return b""
        data = b"".join(collector.chunks)
        if collector.eof or _reader_at_eof(collector.reader):
            collector.eof = True
            if collector.truncated:
                provide.full_body_size = collector.total
            return data
        # Content-Length 描述的是编码后的载荷，因此它无法证明
        # gzip/br/deflate 响应的完整性。
        if (
            expected is not None
            and content_encoding in ("", "identity")
            and len(data) >= expected
        ):
            if collector.truncated:
                provide.full_body_size = collector.total
            return data
        return None

    provide.truncated = False
    provide.full_body_size = None
    return provide


def _response_from_aiohttp(
    response,
    body: Optional[bytes] = None,
    streaming: bool = False,
    body_provider=None,
) -> ResponseRecord:
    headers = [(str(key), str(value)) for key, value in response.headers.items()]
    status = response.status
    if (
        body is None
        and streaming
        and (status in (204, 304) or _header(headers, "content-length") == "0")
    ):
        body = b""
        streaming = False
    return ResponseRecord(
        status=status,
        status_text=response.reason or "",
        headers=headers,
        body=body,
        mime_type=_header(headers, "content-type"),
        http_version=_aiohttp_http_version(getattr(response, "version", None)),
        streaming=streaming,
        redirect_url=_redirect_url(status, headers),
        body_provider=body_provider,
    )


def _register_reader(response) -> Optional[_BodyCollector]:
    try:
        reader = response.content
    except Exception:
        return None
    collector = _BodyCollector(_body_limit())
    collector.reader = reader
    # 保持对读取器的强引用，使其 id() 在捕获仍在进行期间
    # 不会被复用。
    _reader_collectors[id(reader)] = (reader, collector)
    return collector


def _make_aiohttp_client_request_init(original):
    def __init__(self, *args, **kwargs):
        original(self, *args, **kwargs)
        if _aiohttp_body_size.get() is not None:
            return
        try:
            size = getattr(self.body, "size", None)
        except Exception:
            size = None
        if size is not None:
            _aiohttp_body_size.set(int(size))

    return __init__


def _apply_aiohttp_body_size(payload: _AiohttpPayload) -> None:
    if payload.parts is None or payload.size is not None:
        return
    measured = _aiohttp_body_size.get()
    if isinstance(measured, int):
        payload.size = measured


def _make_aiohttp_request(original):
    async def _request(self, method, str_or_url, **kwargs):
        recorder = _active()
        if recorder is None or not recorder.is_capturing():
            return await original(self, method, str_or_url, **kwargs)

        started_at = datetime.now(timezone.utc)
        start = time.perf_counter()
        # FormData 会在 aiohttp 构建载荷时被消费，因此在真实请求运行前
        # 先获取体的描述。
        payload = _aiohttp_body_from_kwargs(kwargs)

        # 在仍然记录响应的同时保留 aiohttp 的 raise_for_status 语义：
        # 先让库不要抛出异常，然后由我们自己抛出。
        requested_raise = kwargs.get("raise_for_status")
        if requested_raise is None:
            requested_raise = getattr(self, "_raise_for_status", None)
        raise_callable = requested_raise if callable(requested_raise) else None
        handled_raise = requested_raise is True or raise_callable is not None
        if handled_raise:
            kwargs["raise_for_status"] = False

        async def record_error_chain(exc) -> None:
            history = tuple(getattr(exc, "history", ()) or ())
            if not history:
                recorder.record(
                    Exchange(
                        request=await _request_from_aiohttp_kwargs(
                            self, method, str_or_url, kwargs, payload, exc
                        ),
                        started_at=started_at,
                        elapsed_ms=_elapsed_ms(start),
                        error=_format_error(exc),
                    )
                )
                return
            elapsed = _elapsed_ms(start)
            for index, hop in enumerate(history):
                is_last = index == len(history) - 1
                recorder.record(
                    Exchange(
                        request=_aiohttp_hop_request(hop, index, payload),
                        started_at=started_at,
                        elapsed_ms=elapsed if is_last else 0.0,
                        response=_response_from_aiohttp(
                            hop, body=None, streaming=True
                        ),
                        error=_format_error(exc) if is_last else None,
                        # 携带 history 的异常意味着重定向链没能走完。
                        redirects_truncated=is_last,
                    )
                )

        size_token = _aiohttp_body_size.set(None)
        try:
            response = await original(self, method, str_or_url, **kwargs)
        except BaseException as exc:
            _apply_aiohttp_body_size(payload)
            await record_error_chain(exc)
            raise
        finally:
            _apply_aiohttp_body_size(payload)
            _aiohttp_body_size.reset(size_token)

        elapsed = _elapsed_ms(start)
        hops = list(getattr(response, "history", None) or []) + [response]
        collector = _register_reader(response)
        for index, hop in enumerate(hops):
            is_final = hop is response
            request_record = _aiohttp_hop_request(hop, index, payload)
            provider = None
            if is_final and collector is not None:
                provider = _aiohttp_body_provider(response, collector)
            recorder.record(
                Exchange(
                    request=request_record,
                    started_at=started_at,
                    elapsed_ms=elapsed if is_final else 0.0,
                    response=_response_from_aiohttp(
                        hop,
                        body=None,
                        # 重定向各跳的体不会被读取（那会破坏流式传输）；
                        # 将它们标记为未知，而不是声称为 0。
                        streaming=not is_final,
                        body_provider=provider,
                    ),
                )
            )

        if handled_raise:
            if raise_callable is not None:
                raise_callable(response)
            else:
                # 对错误状态抛出 ClientResponseError 并释放连接，
                # 与 aiohttp 内部原本的行为保持一致。
                response.raise_for_status()
        return response

    return _request
