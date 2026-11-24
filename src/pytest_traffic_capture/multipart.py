"""尽力解析 multipart/form-data 请求体。"""

from __future__ import annotations

from dataclasses import dataclass
from email.parser import BytesParser
from email.policy import default
from typing import List, Optional
from urllib.parse import unquote


def _decode_filename(filename: Optional[str]) -> Optional[str]:
    """还原 aiohttp 在线上对文件名做的百分号编码。

    线上会写成 ``filename="%E6%96%87%E4%BB%B6.txt"``，而调用方传入的是
    ``文件.txt``。仅当名字里确实含百分号转义时才解码；解码失败则原样返回。
    """
    if not filename or not isinstance(filename, str) or "%" not in filename:
        return filename
    try:
        decoded = unquote(filename, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return filename
    return decoded or filename


@dataclass
class Part:
    name: str
    value: bytes
    filename: Optional[str] = None
    content_type: Optional[str] = None
    unreadable: bool = False
    truncated: bool = False
    size: Optional[int] = None
    source: str = ""


def parse_multipart(body: bytes, content_type: str) -> Optional[List[Part]]:
    """使用 *content_type* 中声明的 boundary 解析 *body*。

    当载荷不是标准库能够解析的 multipart 体时返回 ``None``。
    遇到格式错误的数据绝不抛出异常。
    """
    if not body or "multipart/" not in (content_type or "").lower():
        return None
    if "boundary=" not in (content_type or "").lower():
        return None
    pseudo = (
        b"Content-Type: "
        + content_type.encode("latin-1", "replace")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + body
    )
    try:
        message = BytesParser(policy=default).parsebytes(pseudo)
    except Exception:
        return None
    if not message.is_multipart():
        return None
    parts: List[Part] = []
    try:
        subparts = list(message.iter_parts())
    except Exception:
        return None
    for sub in subparts:
        try:
            name = sub.get_param("name", header="content-disposition") or ""
            filename = sub.get_filename()
            # 保留完整的头部值（包括 ``charset`` 之类的参数）；
            # ``get_content_type()`` 会丢弃这些参数，并且当分段没有
            # Content-Type 时还会凭空造出 ``text/plain``。
            raw_type = sub.get("Content-Type")
            part_type = " ".join(str(raw_type).split()) if raw_type else None
            payload = sub.get_payload(decode=True)
            if payload is None:
                raw = sub.get_payload()
                payload = raw.encode("utf-8", "replace") if isinstance(raw, str) else b""
        except Exception:
            continue
        parts.append(
            Part(
                name=str(name),
                value=payload,
                filename=str(_decode_filename(filename))
                if filename is not None
                else None,
                content_type=part_type,
            )
        )
    return parts or None
