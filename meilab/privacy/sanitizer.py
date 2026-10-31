"""三层脱敏:日志层 / 旁路层 / 产物层。

**为什么需要**:抓回来的页面里可能含手机号、邮箱、IP,以及带时效签名的 URL 参数。
这些一旦进日志或导出文件就会外泄。

**三层**:
1. **日志层**:给 logger 装 filter,日志里出现的敏感串被替换成掩码;
2. **旁路层**:`print` 桥、GUI/CLI 输出、异常回显同样要过一遍;
3. **产物层**:导出前对整条记录递归脱敏(深度 6 层)。

**明确边界(与 kiana 一致)**:**任务队列里的 URL 不脱敏**——它要作为后续请求的凭据,
抹掉签名参数会让续爬直接 403。这条边界写死在 :func:`sanitize_url` 的调用约定里:
队列路径调用 :func:`redact_url_params` 的**白名单式**记录(只标记不删除),
只有产物层才做真正的替换。

敏感对象:手机号、邮箱、IP、19 类敏感 URL 参数。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Iterable

#: 手机号(中国大陆 11 位,允许 +86 前缀)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)")

#: 邮箱
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

#: IPv4(排除版本号样式)
IPV4_RE = re.compile(r"(?<![\w.])((?:\d{1,3}\.){3}\d{1,3})(?![\w.])")

#: 需要从 URL 里抹掉的敏感参数(19 类)
SENSITIVE_QUERY_KEYS = (
    "token", "access_token", "refresh_token", "id_token", "session", "sessionid",
    "sid", "sign", "signature", "auth", "authorization", "jwt", "ticket",
    "secret", "password", "passwd", "pwd", "api_key", "apikey", "key",
    "code", "credential", "sess", "sp", "policy", "wsSecret", "wts", "w_rid",
)

#: 掩码后的占位符
MASK = "***"

#: 记录递归脱敏的最大深度
MAX_DEPTH = 6

#: URL 参数名 → 脱敏后的值(队列里记录用,不丢参数名)
_URL_PARAM_RE = re.compile(r"([?&])([A-Za-z0-9_\-\[\]]+)=([^&#]*)")


def mask_phone(text: str) -> str:
    """掩码手机号(保留前 3 后 4 位)。"""
    def repl(match: re.Match[str]) -> str:
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) < 7:
            return MASK
        return f"{digits[:3]}****{digits[-4:]}"

    return PHONE_RE.sub(repl, text)


def mask_email(text: str) -> str:
    """掩码邮箱(保留首字符与域名)。"""
    def repl(match: re.Match[str]) -> str:
        local, _, domain = match.group(0).partition("@")
        keep = local[:1] if local else ""
        return f"{keep}{MASK}@{domain}"

    return EMAIL_RE.sub(repl, text)


def mask_ip(text: str) -> str:
    """掩码 IPv4(保留网段,末段掩码)。"""
    def repl(match: re.Match[str]) -> str:
        parts = match.group(1).split(".")
        if len(parts) != 4:
            return match.group(0)
        return f"{parts[0]}.{parts[1]}.{parts[2]}.{MASK}"

    return IPV4_RE.sub(repl, text)


def sanitize_text(text: str) -> str:
    """脱敏一段自由文本(手机号 / 邮箱 / IP)。

    :param text: 原文。
    :return: 脱敏后的文本。
    """
    if not text:
        return text
    result = mask_phone(text)
    result = mask_email(result)
    result = mask_ip(result)
    return result


def redact_url_params(url: str, *, keep_names: bool = True) -> str:
    """把 URL 里的敏感参数值替换成掩码。

    :param url: 原始 URL。
    :param keep_names: 保留参数名(只抹值);``False`` 则整个参数删掉。
    :return: 处理后的 URL。
    """
    if not url or "?" not in url:
        return url
    lowered = url.lower()

    def repl(match: re.Match[str]) -> str:
        name = match.group(2)
        if name.lower() not in SENSITIVE_QUERY_KEYS:
            return match.group(0)
        if keep_names:
            return f"{match.group(1)}{name}={MASK}"
        return ""

    return _URL_PARAM_RE.sub(repl, url)


def sanitize_url(url: str, *, keep_names: bool = True) -> str:
    """脱敏一个 URL(参数 + 文本里的手机号/邮箱/IP)。

    :param url: 原始 URL。
    :param keep_names: 保留参数名。
    :return: 脱敏后的 URL。
    """
    if not url:
        return url
    return sanitize_text(redact_url_params(url, keep_names=keep_names))


def sanitize_record(value: Any, _depth: int = 0) -> Any:
    """递归脱敏一条记录里的所有字符串。

    :param value: 任意结构(字典 / 列表 / 标量)。
    :param _depth: 递归深度(内部用)。
    :return: 脱敏后的结构。
    """
    if _depth > MAX_DEPTH:
        return value
    if isinstance(value, str):
        return sanitize_text(value)
    if isinstance(value, dict):
        return {
            key: sanitize_record(item, _depth + 1) for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        cleaned = [sanitize_record(item, _depth + 1) for item in value]
        return type(value)(cleaned) if isinstance(value, tuple) else cleaned
    return value


class SanitizingFilter(logging.Filter):
    """日志脱敏过滤器(挂在 handler 上,挂在 root logger 上无效)。"""

    def filter(self, record: logging.LogRecord) -> bool:
        """脱敏记录的消息与参数,永远放行(不吞日志)。"""
        try:
            record.msg = sanitize_text(str(record.msg))
            if record.args:
                if isinstance(record.args, dict):
                    record.args = {
                        key: sanitize_text(str(value)) for key, value in record.args.items()
                    }
                elif isinstance(record.args, tuple):
                    record.args = tuple(sanitize_text(str(v)) for v in record.args)
        except Exception:
            pass
        return True


def install_log_filter(logger: logging.Logger | None = None) -> SanitizingFilter:
    """给 logger 装上脱敏过滤器。

    **必须挂在 handler 上**:挂在 logger 上只对经该 logger 创建的 handler 生效,
    而挂在 root logger 上时,子 logger 的 handler 不会继承 filter。

    :param logger: 目标 logger;``None`` 表示 root logger。
    :return: 装好的过滤器(便于移除)。
    """
    target = logger or logging.getLogger()
    flt = SanitizingFilter()
    for handler in target.handlers:
        handler.addFilter(flt)
    return flt


def sanitize_out(text: str) -> str:
    """旁路层出口(CLI / GUI / 异常回显)统一走这里。

    :param text: 待输出文本。
    :return: 脱敏后的文本。
    """
    return sanitize_text(text)


def sensitive_param_names() -> tuple[str, ...]:
    """返回敏感 URL 参数清单(供文档与门禁引用)。"""
    return SENSITIVE_QUERY_KEYS


def iter_sensitive_keys() -> Iterable[str]:
    """迭代敏感参数名。"""
    return iter(SENSITIVE_QUERY_KEYS)
