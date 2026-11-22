"""将捕获的交换渲染为 HAR 1.2 文档。"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence
from urllib.parse import parse_qsl, urljoin, urlsplit

from . import __version__, multipart
from .model import Exchange, RequestRecord, ResponseRecord

SaveBody = Callable[[str, bytes], str]

_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
    "application/graphql",
    "application/x-ndjson",
    "application/x-www-form-urlencoded",
    "application/sql",
    "application/yaml",
    "application/x-yaml",
}

_BINARY_MIME_TYPES = {
    "application/octet-stream",
    "application/pdf",
    "application/zip",
    "application/gzip",
    "application/x-tar",
    "application/x-7z-compressed",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_EXT_BY_MIME = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/svg+xml": ".svg",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/json": ".json",
    "application/xml": ".xml",
    "text/plain": ".txt",
    "text/html": ".html",
}


@dataclass
class BodyAnalysis:
    kind: str = "none"  # none | text | form | multipart | binary | unreadable
    mime_type: str = ""
    text: str = ""
    params: List[dict] = field(default_factory=list)
    file: str = ""
    raw_file: str = ""
    size: int = 0


def _header_value(headers: Sequence, name: str) -> str:
    wanted = name.lower()
    for key, value in headers:
        if str(key).lower() == wanted:
            return str(value)
    return ""


def _split_mime(value: str) -> str:
    return (value or "").split(";")[0].strip().lower()


def _decode_text(data: bytes, mime_type: str) -> Optional[str]:
    """使用 *mime_type* 中的 charset 解码 *data*，失败时回退到 UTF-8。"""
    charset = ""
    for parameter in (mime_type or "").split(";")[1:]:
        key, _, value = parameter.partition("=")
        if key.strip().lower() == "charset":
            charset = value.strip().strip("\"'")
            break
    for encoding in (charset, "utf-8"):
        if not encoding:
            continue
        try:
            return data.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return None


def _is_textual(mime_type: str) -> bool:
    base = _split_mime(mime_type)
    if not base:
        return True
    if base.startswith("text/"):
        return True
    if base in _TEXT_MIME_TYPES or base.endswith("+json") or base.endswith("+xml"):
        return True
    if base.startswith(("image/", "audio/", "video/", "font/")):
        return False
    if base in _BINARY_MIME_TYPES:
        return False
    return True


def _ext_for_mime(mime_type: str) -> str:
    return _EXT_BY_MIME.get(_split_mime(mime_type), ".bin")


def _save_body_file(
    save_body: Optional[SaveBody],
    field_name: str,
    filename: Optional[str],
    data: bytes,
    mime_type: str = "",
) -> str:
    if save_body is None:
        return ""
    hint = field_name or "body"
    if filename:
        suffix = Path(filename).suffix
        if suffix:
            hint = hint + suffix
    elif mime_type:
        hint = hint + _ext_for_mime(mime_type)
    return save_body(hint, data)


def analyze_request(
    request: RequestRecord,
    save_body: Optional[SaveBody] = None,
    max_inline_bytes: int = 0,
) -> BodyAnalysis:
    """对请求体仅分析一次（并写入旁挂文件(sidecar)）。"""
    if request.analysis is not None:
        return request.analysis
    analysis = _analyze_request(request, save_body, max_inline_bytes)
    request.analysis = analysis
    return analysis


def _oversized_body(
    body: bytes,
    size: int,
    mime_type: str,
    save_body: Optional[SaveBody],
    max_inline_bytes: int,
) -> Optional[BodyAnalysis]:
    if save_body is None:
        # 没有旁挂文件(sidecar)接收器的库调用方仍必须拿到体。
        return None
    if not max_inline_bytes or max_inline_bytes <= 0 or size <= max_inline_bytes:
        return None
    rel = _save_body_file(save_body, "body", None, body, mime_type)
    return BodyAnalysis(kind="binary", mime_type=mime_type, file=rel, size=size)


def _raw_sidecar(
    body: bytes,
    text: str,
    save_body: Optional[SaveBody],
    mime_type: str,
) -> str:
    """当解码后的文本与原始字节不一致时，保存原始字节。"""
    if save_body is None:
        return ""
    try:
        changed = body != text.encode("utf-8")
    except Exception:
        changed = True
    if not changed:
        return ""
    return _save_body_file(save_body, "body", None, body, mime_type)


def _field_item(part, save_body: Optional[SaveBody]) -> dict:
    """描述一个非文件的 multipart 字段，且不丢失原始字节。"""
    raw = part.value
    text = raw.decode("utf-8", "replace")
    item = {"name": part.name, "value": text}
    if part.content_type:
        item["contentType"] = part.content_type
    if getattr(part, "truncated", False):
        item["_truncated"] = True
        if part.size is not None:
            item["_size"] = part.size
    needs_sidecar = raw != text.encode("utf-8")
    if save_body is not None and needs_sidecar:
        rel = _save_body_file(
            save_body, part.name, None, raw, part.content_type or ""
        )
        if rel:
            item["_file"] = rel
    return item


def _analyze_request(
    request: RequestRecord,
    save_body: Optional[SaveBody],
    max_inline_bytes: int = 0,
) -> BodyAnalysis:
    mime_type = _header_value(request.headers, "content-type")

    if request.unreadable:
        return BodyAnalysis(kind="unreadable", mime_type=mime_type)

    if request.parts is not None:
        params: List[dict] = []
        for part in request.parts:
            if getattr(part, "unreadable", False):
                item = {
                    "name": part.name,
                    "value": "",
                    "fileName": part.filename or "",
                    "contentType": (
                        part.content_type or "application/octet-stream"
                    ),
                    "_unreadable": True,
                }
                if part.size is not None:
                    item["_size"] = part.size
                if part.source:
                    item["_source"] = part.source
                params.append(item)
                continue
            if part.filename is not None:
                rel = _save_body_file(
                    save_body, part.name, part.filename, part.value, part.content_type or ""
                )
                item = {
                    "name": part.name,
                    "value": "",
                    "fileName": part.filename,
                    "contentType": part.content_type or "application/octet-stream",
                }
                if rel:
                    item["_file"] = rel
                if getattr(part, "truncated", False):
                    item["_truncated"] = True
                    if part.size is not None:
                        item["_size"] = part.size
                params.append(item)
            else:
                params.append(_field_item(part, save_body))
        # aiohttp 在 ClientRequest 构建载荷后报告编码后的 multipart 大小；
        # 当大小无法测量时回退为 -1。
        return BodyAnalysis(
            kind="multipart",
            mime_type=mime_type,
            params=params,
            size=request.body_size if request.body_size is not None else -1,
        )

    body = request.body
    if body is None:
        return BodyAnalysis(kind="none", mime_type=mime_type)

    size = len(body)

    if "multipart/form-data" in mime_type.lower() and "boundary=" in mime_type.lower():
        parts = multipart.parse_multipart(body, mime_type)
        if parts:
            params = []
            for part in parts:
                if part.filename is not None:
                    rel = _save_body_file(
                        save_body,
                        part.name,
                        part.filename,
                        part.value,
                        part.content_type or "",
                    )
                    item = {
                        "name": part.name,
                        "value": "",
                        "fileName": part.filename,
                        "contentType": part.content_type or "application/octet-stream",
                    }
                    if rel:
                        item["_file"] = rel
                    if getattr(part, "truncated", False):
                        item["_truncated"] = True
                        if part.size is not None:
                            item["_size"] = part.size
                    params.append(item)
                else:
                    params.append(_field_item(part, save_body))
            return BodyAnalysis(
                kind="multipart", mime_type=mime_type, params=params, size=size
            )

    if _split_mime(mime_type) == "application/x-www-form-urlencoded":
        oversized = _oversized_body(
            body, size, mime_type, save_body, max_inline_bytes
        )
        if oversized is not None:
            return oversized
        text = body.decode("utf-8", "replace")
        params = [
            {"name": key, "value": value}
            for key, value in parse_qsl(text, keep_blank_values=True)
        ]
        analysis = BodyAnalysis(
            kind="form", mime_type=mime_type, text=text, params=params, size=size
        )
        analysis.raw_file = _raw_sidecar(body, text, save_body, mime_type)
        return analysis

    if _is_textual(mime_type):
        text = _decode_text(body, mime_type)
        if text is not None:
            oversized = _oversized_body(
                body, size, mime_type, save_body, max_inline_bytes
            )
            if oversized is not None:
                return oversized
            analysis = BodyAnalysis(
                kind="text", mime_type=mime_type, text=text, size=size
            )
            analysis.raw_file = _raw_sidecar(body, text, save_body, mime_type)
            return analysis

    rel = _save_body_file(save_body, "body", None, body, mime_type)
    return BodyAnalysis(kind="binary", mime_type=mime_type, file=rel, size=size)


def _headers_to_har(headers: Sequence) -> List[dict]:
    return [{"name": str(key), "value": str(value)} for key, value in headers]


def _query_string(url: str) -> List[dict]:
    try:
        query = urlsplit(url).query
        pairs = parse_qsl(query, keep_blank_values=True)
    except Exception:
        return []
    return [{"name": key, "value": value} for key, value in pairs]


def _request_cookies(headers: Sequence) -> List[dict]:
    raw = _header_value(headers, "cookie")
    if not raw:
        return []
    cookies: List[dict] = []
    try:
        jar = SimpleCookie()
        jar.load(raw)
        for name, morsel in jar.items():
            cookies.append({"name": name, "value": morsel.value})
    except CookieError:
        for chunk in raw.split(";"):
            if "=" in chunk:
                name, _, value = chunk.partition("=")
                if name.strip():
                    cookies.append({"name": name.strip(), "value": value.strip()})
    return cookies


def _response_cookies(headers: Sequence) -> List[dict]:
    cookies: List[dict] = []
    for key, value in headers:
        if str(key).lower() != "set-cookie":
            continue
        try:
            jar = SimpleCookie()
            jar.load(str(value))
            for name, morsel in jar.items():
                cookies.append({"name": name, "value": morsel.value})
        except CookieError:
            continue
    return cookies


def _request_entry(
    request: RequestRecord,
    save_body: Optional[SaveBody],
    max_inline_bytes: int = 0,
) -> Dict:
    analysis = analyze_request(request, save_body, max_inline_bytes)
    if request.truncated:
        body_size = (
            request.full_body_size
            if request.full_body_size is not None
            else -1
        )
    elif analysis.kind == "none":
        body_size = request.body_size if request.body_size is not None else 0
    elif analysis.kind == "unreadable":
        body_size = request.body_size if request.body_size is not None else -1
    else:
        body_size = analysis.size
    mime_type = analysis.mime_type or ""
    entry: Dict = {
        "method": request.method,
        "url": request.url,
        "httpVersion": request.http_version or "HTTP/1.1",
        "headers": _headers_to_har(request.headers),
        "queryString": _query_string(request.url),
        "cookies": _request_cookies(request.headers),
        "headersSize": -1,
        "bodySize": body_size,
    }
    if request.body_source:
        entry["_body_source"] = request.body_source
    if request.truncated:
        entry["_truncated"] = True
        if request.full_body_size is not None:
            entry["_size"] = request.full_body_size
    if analysis.kind == "text":
        entry["postData"] = {
            "mimeType": mime_type,
            "text": analysis.text,
        }
        if analysis.raw_file:
            entry["postData"]["_file"] = analysis.raw_file
    elif analysis.kind == "form":
        entry["postData"] = {
            "mimeType": mime_type,
            "params": analysis.params,
        }
        if analysis.raw_file:
            entry["postData"]["_file"] = analysis.raw_file
    elif analysis.kind == "multipart":
        entry["postData"] = {
            "mimeType": mime_type,
            "params": analysis.params,
        }
    elif analysis.kind == "binary":
        if analysis.file:
            entry["postData"] = {
                "mimeType": mime_type,
                "_file": analysis.file,
            }
        elif request.body is not None:
            # 没有旁挂文件(sidecar)接收器（库用法）：把字节保留在 HAR 中。
            entry["postData"] = {
                "mimeType": mime_type,
                "text": base64.b64encode(request.body).decode("ascii"),
                "_encoding": "base64",
            }
        else:
            entry["postData"] = {"mimeType": mime_type}
    if analysis.kind == "unreadable":
        entry["_unreadable_body"] = True
    return entry


def _empty_response_entry() -> Dict:
    return {
        "status": 0,
        "statusText": "",
        "httpVersion": "HTTP/1.1",
        "headers": [],
        "cookies": [],
        "content": {"size": 0, "mimeType": ""},
        "redirectURL": "",
        "headersSize": -1,
        "bodySize": 0,
    }


def _response_entry(
    response: Optional[ResponseRecord],
    max_inline_bytes: int,
    save_body: Optional[SaveBody],
    request_url: Optional[str] = None,
    request_method: Optional[str] = None,
) -> Dict:
    if response is None:
        return _empty_response_entry()

    headers = _headers_to_har(response.headers)
    status = int(response.status)
    redirect_url = ""
    if 300 <= status < 400:
        redirect_url = _header_value(response.headers, "location") or response.redirect_url or ""
        if redirect_url and request_url:
            redirect_url = urljoin(request_url, redirect_url)

    content: Dict = {
        "size": 0,
        "mimeType": response.mime_type or "",
    }
    body = response.body
    if body is None and response.body_provider is not None:
        # 流式客户端：仅当调用方消费体时（或在其消费期间）才会收集
        # 该体。
        body = response.body_provider()
        response.streaming = body is None
        response.truncated = bool(
            getattr(response.body_provider, "truncated", False)
        )
        response.full_body_size = getattr(
            response.body_provider, "full_body_size", None
        )
    if response.streaming:
        content["size"] = -1
        body_size = -1
        if response.truncated:
            content["_truncated"] = True
            if response.full_body_size is not None:
                content["_size"] = response.full_body_size
    elif body is None:
        body_size = 0
    else:
        size = len(body)
        content["size"] = size
        if response.truncated:
            content["_truncated"] = True
            if response.full_body_size is not None:
                content["_size"] = response.full_body_size
            body_size = -1
        else:
            # HAR bodySize 是线上大小。客户端会透明地解压 gzip/br
            # 响应，因此存在 Content-Length 时使用它，否则使用解码后的
            # 载荷长度。HEAD、204 和 304 没有响应体，所以它们的
            # Content-Length 描述的是一个假想的 GET，绝不能作为线上
            # 大小报告。
            method = str(request_method or "").upper()
            if method == "HEAD" or status in (204, 304):
                body_size = size
            else:
                encoding = _header_value(
                    response.headers, "content-encoding"
                ).strip().lower()
                if encoding in ("", "identity"):
                    body_size = size
                else:
                    try:
                        encoded_size = int(
                            _header_value(response.headers, "content-length")
                        )
                    except (TypeError, ValueError):
                        encoded_size = None
                    if encoded_size is None:
                        body_size = -1
                    else:
                        body_size = encoded_size
                        # HAR 1.2 将 ``compression`` 定义为节省的字节数，
                        # 因此当线上体小于解码后的内容时它为
                        # 正值。
                        content["compression"] = size - encoded_size
        textual = _is_textual(response.mime_type)
        if max_inline_bytes and max_inline_bytes > 0 and size > max_inline_bytes:
            rel = _save_body_file(save_body, "response", None, body, response.mime_type)
            if rel:
                content["_file"] = rel
            elif textual:
                text = _decode_text(body, response.mime_type)
                if text is not None:
                    content["text"] = text
                else:
                    content["text"] = base64.b64encode(body).decode("ascii")
                    content["encoding"] = "base64"
            else:
                content["text"] = base64.b64encode(body).decode("ascii")
                content["encoding"] = "base64"
        elif textual:
            text = _decode_text(body, response.mime_type)
            if text is not None:
                content["text"] = text
            else:
                content["text"] = base64.b64encode(body).decode("ascii")
                content["encoding"] = "base64"
        else:
            content["text"] = base64.b64encode(body).decode("ascii")
            content["encoding"] = "base64"

    return {
        "status": status,
        "statusText": response.status_text or "",
        "httpVersion": response.http_version or "HTTP/1.1",
        "headers": headers,
        "cookies": _response_cookies(response.headers),
        "content": content,
        "redirectURL": redirect_url,
        "headersSize": -1,
        "bodySize": body_size,
    }


def _isoformat(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.isoformat(timespec="milliseconds")


def build_har(
    exchanges: Sequence[Exchange],
    *,
    max_inline_bytes: int = 1048576,
    save_body: Optional[SaveBody] = None,
    creator: Optional[Dict] = None,
) -> Dict:
    entries: List[Dict] = []
    for exchange in exchanges:
        wait_ms = round(exchange.elapsed_ms, 3)
        entry: Dict = {
            "startedDateTime": _isoformat(exchange.started_at),
            "time": wait_ms,
            "request": _request_entry(
                exchange.request, save_body, max_inline_bytes
            ),
            "response": _response_entry(
                exchange.response,
                max_inline_bytes,
                save_body,
                exchange.request.url if exchange.request is not None else None,
                (
                    exchange.request.method
                    if exchange.request is not None
                    else None
                ),
            ),
            "cache": {},
            "timings": {"send": 0, "wait": wait_ms, "receive": 0},
        }
        if exchange.request is not None and exchange.request.not_sent:
            entry["request"]["_not_sent"] = True
        if (
            exchange.request is not None
            and exchange.request.headers_synthesized
        ):
            entry["request"]["_headers_synthesized"] = True
        if exchange.response is not None and exchange.response.streaming:
            entry["_streaming"] = True
        if exchange.error:
            entry["_error"] = exchange.error
        if exchange.redirects_truncated:
            entry["_redirects_truncated"] = True
        entries.append(entry)
    return {
        "log": {
            "version": "1.2",
            "creator": creator
            or {"name": "pytest-traffic-capture", "version": __version__},
            "entries": entries,
        }
    }
