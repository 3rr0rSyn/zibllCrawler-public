"""通用输入校验工具。

为导入模块、主程序等提供 URL/域名/账号密码等输入的安全过滤，
所有数据库写入仍由调用方使用参数化查询完成，本模块只做前置校验。
"""

import ipaddress
import json
import re
from typing import List, Optional
from urllib.parse import urlparse


# 允许的 URL scheme
_ALLOWED_SCHEMES = {"http", "https"}

# 域名标签允许字符：字母、数字、连字符、下划线（较宽松）
_DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-_]*[A-Za-z0-9])?$")

# 标识符允许字符：字母、数字、下划线、点（用于 module/func/task_name）
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_\.]+$")

# 控制字符过滤（保留普通可打印字符与空白）
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")


class ValidationError(ValueError):
    """校验失败时抛出的异常，携带可读的失败原因。"""

    def __init__(self, field: str, message: str):
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def normalize_url(url: str) -> str:
    """补全协议并去掉路径，返回 scheme://host 形式。"""
    url = url.strip()
    parsed = urlparse(url)
    if not parsed.scheme:
        url = f"https://{url}"
        parsed = urlparse(url)
    netloc = parsed.netloc
    if not netloc:
        return url
    return f"{parsed.scheme}://{netloc}"


def is_valid_url(url: str, allow_private: bool = False) -> bool:
    """校验 URL 是否合法。"""
    url = url.strip()
    parsed = urlparse(url)
    if not parsed.scheme or parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    if not parsed.netloc:
        return False
    if "@" in parsed.netloc or ":" in parsed.netloc.rsplit(":", 1)[0]:
        # 拒绝 user:pass@host 或 IPv6 以外奇怪的端口写法，简化处理
        return False
    return is_valid_host(parsed.netloc, allow_private=allow_private)


def is_valid_host(host: str, allow_private: bool = False) -> bool:
    """校验 host 是合法域名或可访问 IP。"""
    host = host.strip()
    if not host:
        return False

    # 简单去掉端口
    if ":" in host and host.count(":") == 1:
        host_part, port_part = host.rsplit(":", 1)
        if port_part.isdigit():
            host = host_part

    # IPv6 中括号
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]

    # 尝试解析为 IP
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_loopback or ip.is_private or ip.is_reserved or ip.is_unspecified:
            return bool(allow_private)
        return True
    except ValueError:
        pass

    # 域名校验
    labels = host.split(".")
    if len(labels) < 2:
        return False
    for label in labels:
        if not _DOMAIN_LABEL_RE.match(label) or len(label) > 63:
            return False
    if len(host) > 253:
        return False
    return True


def parse_aliases(raw: Optional[str]) -> List[str]:
    """解析逗号/空格/换行分隔的别名，返回规范化后的合法 URL 列表。"""
    if not raw:
        return []
    aliases = []
    for part in re.split(r"[,\s]+", raw.strip()):
        part = part.strip()
        if not part:
            continue
        normalized = normalize_url(part)
        if not is_valid_url(normalized):
            raise ValidationError("aliases", f"别名 URL 不合法: {part}")
        aliases.append(normalized)
    return aliases


def parse_json_aliases(value: Optional[str]) -> List[str]:
    """从 JSON 字符串解析别名列表（用于读取数据库）。"""
    if not value:
        return []
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValidationError("aliases", f"别名 JSON 解析失败: {exc}") from exc
    if not isinstance(data, list):
        raise ValidationError("aliases", "别名必须是 JSON 数组")
    result = []
    for item in data:
        if not isinstance(item, str):
            raise ValidationError("aliases", "别名数组元素必须是字符串")
        normalized = normalize_url(item)
        if not is_valid_url(normalized):
            raise ValidationError("aliases", f"别名 URL 不合法: {item}")
        result.append(normalized)
    return result


def serialize_aliases(aliases: List[str]) -> str:
    """将别名列表序列化为 JSON 字符串。"""
    return json.dumps(aliases, ensure_ascii=False)


def sanitize_identifier(value: str, field: str, max_len: int = 100) -> str:
    """清理 task_name / module / func 等标识符。"""
    value = value.strip()
    if not value:
        raise ValidationError(field, "不能为空")
    if len(value) > max_len:
        raise ValidationError(field, f"长度不能超过 {max_len}")
    value = _CONTROL_CHAR_RE.sub("", value)
    if not _IDENTIFIER_RE.match(value):
        raise ValidationError(field, "只能包含字母、数字、下划线和点")
    return value


def sanitize_username(value: str, max_len: int = 100) -> str:
    """清理用户名：允许常见字符，过滤控制字符与 SQL 注入风险字符。"""
    value = value.strip()
    if not value:
        raise ValidationError("username", "用户名不能为空")
    if len(value) > max_len:
        raise ValidationError("username", f"用户名长度不能超过 {max_len}")
    value = _CONTROL_CHAR_RE.sub("", value)
    # 禁止可能导致 SQL 注入或路径穿越的字符
    forbidden = {";", "--", "/*", "*/", "\\", "\x00"}
    for ch in forbidden:
        if ch in value:
            raise ValidationError("username", f"包含非法字符: {ch!r}")
    return value


def sanitize_password(value: str, max_len: int = 255) -> str:
    """清理密码：过滤控制字符，不过度限制合法复杂密码。"""
    if not value:
        raise ValidationError("password", "密码不能为空")
    if len(value) > max_len:
        raise ValidationError("password", f"密码长度不能超过 {max_len}")
    cleaned = _CONTROL_CHAR_RE.sub("", value)
    if "\x00" in cleaned:
        raise ValidationError("password", "包含非法空字符")
    return cleaned


def sanitize_url(value: str, field: str = "url") -> str:
    """校验并规范化 URL，失败抛出 ValidationError。"""
    value = value.strip()
    if not value:
        raise ValidationError(field, "URL 不能为空")
    normalized = normalize_url(value)
    if not is_valid_url(normalized):
        raise ValidationError(field, f"URL 不合法: {value}")
    return normalized
