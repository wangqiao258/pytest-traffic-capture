"""描述捕获的 HTTP 交换的数据类。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional, Tuple


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RequestRecord:
    method: str
    url: str
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: Optional[bytes] = None
    http_version: str = "HTTP/1.1"
    unreadable: bool = False
    # 无法原样捕获的体的已知实际传输大小。
    body_size: Optional[int] = None
    body_source: str = ""
    # 当请求从未交给传输层时为 True，例如 httpx 的 TooManyRedirects
    # 异常所携带的下一个请求。
    not_sent: bool = False
    # 当头部是在本地重建、而非从客户端的请求对象读取时为 True
    # （aiohttp 的错误路径）。
    headers_synthesized: bool = False
    # 当请求体是按 kwargs 推导、而非从写入点观测时为 True
    # （aiohttp 的 ``write_bytes`` 未被调用，或会话级序列化器覆盖了默认行为）。
    body_synthesized: bool = False
    # 当体来自 aiohttp 的线上字节、其中 multipart 文件名仍处于百分号编码状态
    # 时为 True；导出器只在此时还原文件名，避免影响 requests/httpx。
    percent_encoded_filenames: bool = False
    # 预先解析的 multipart 字段（供 aiohttp.FormData 使用，该库会在
    # 请求交给传输层之前消费掉它）。
    parts: Optional[List[Any]] = None
    # 由导出器生成的体分析缓存（内部使用）。
    analysis: Optional[Any] = field(default=None, repr=False, compare=False)
    # 当捕获上限只保留体的前 N 个字节时置位。
    truncated: bool = False
    full_body_size: Optional[int] = None


@dataclass
class ResponseRecord:
    status: int
    status_text: str = ""
    headers: List[Tuple[str, str]] = field(default_factory=list)
    body: Optional[bytes] = None
    mime_type: str = ""
    http_version: str = "HTTP/1.1"
    streaming: bool = False
    redirect_url: str = ""
    # 懒收集的体（仅 aiohttp）：在写出产物时调用。
    # 当调用方从未消费响应体时返回 ``None``。
    body_provider: Optional[Callable[[], Optional[bytes]]] = field(
        default=None, repr=False, compare=False
    )
    # 当捕获上限只保留体的前 N 个字节时置位。
    truncated: bool = False
    full_body_size: Optional[int] = None


@dataclass
class Exchange:
    request: RequestRecord
    started_at: datetime = field(default_factory=_utcnow)
    elapsed_ms: float = 0.0
    response: Optional[ResponseRecord] = None
    error: Optional[str] = None
    # 当重定向链无法被完整记录时为 True。
    redirects_truncated: bool = False
