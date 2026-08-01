"""全局代理配置模块。

提供统一的代理设置、可用性测试与清理能力。
通过环境变量 `HTTP_PROXY` / `HTTPS_PROXY`（及小写形式）影响所有 `requests` 请求，
无需在每个 `requests.Session()` 中单独注入。

支持行为：
- 代理测试成功：全局启用代理。
- 代理测试失败：根据配置结束运行或清除代理继续运行。
"""

import logging
import os
import re
from typing import Optional, Tuple

import requests


logger = logging.getLogger("zibllcrawler.core.proxy_config")

DEFAULT_TIMEOUT = 10


class ProxyConfig:
    """全局代理配置。"""

    def __init__(
        self,
        proxy_url: str,
        test_url: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.raw = proxy_url.strip()
        self.http_url, self.https_url = self._normalize(self.raw)
        self.test_url = test_url
        self.timeout = timeout

    @staticmethod
    def _normalize(raw: str) -> Tuple[str, str]:
        """
        将用户输入统一为代理 URL。

        示例：
        - host:port -> http://host:port
        - http://host:port -> 保持不变
        - socks5://host:port -> 保持不变
        """
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://", raw):
            raw = f"http://{raw}"
        # 同时支持 http 与 https，requests 会根据请求协议选择
        return raw, raw

    def apply(self) -> None:
        """设置环境变量，使所有 requests 请求自动使用代理。"""
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ[key] = self.https_url if key.startswith("HTTPS") else self.http_url
        logger.info(f"已设置全局代理: {self.http_url}")

    def test(self) -> bool:
        """通过代理访问测试 URL，验证代理是否可用；未提供测试 URL 时跳过测试。"""
        if not self.test_url:
            logger.info("未提供代理测试 URL，跳过可用性测试")
            return True

        proxies = {"http": self.http_url, "https": self.https_url}
        try:
            resp = requests.get(self.test_url, proxies=proxies, timeout=self.timeout)
            logger.info(f"代理测试成功: {self.test_url} -> HTTP {resp.status_code}")
            return True
        except requests.exceptions.ProxyError as exc:
            logger.error(f"代理连接失败: {exc}")
        except requests.exceptions.Timeout:
            logger.error(f"代理测试超时: {self.test_url}")
        except requests.exceptions.RequestException as exc:
            logger.error(f"代理测试请求失败: {exc}")
        except Exception as exc:
            logger.error(f"代理测试未知异常: {exc}")
        return False

    def clear(self) -> None:
        """清除环境变量中的代理设置。"""
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            os.environ.pop(key, None)
        logger.info("已清除全局代理设置")


def configure_proxy(
    proxy: str,
    fail_action: str = "exit",
    test_url: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> bool:
    """
    配置全局代理并测试。

    Args:
        proxy: 代理地址，如 host:port 或 http://host:port。
        fail_action: 代理测试失败时的行为：'exit' 结束运行，'continue' 禁用代理继续运行。
        test_url: 代理可用性测试 URL；不提供则跳过可用性测试。
        timeout: 测试超时时间（秒）。

    Returns:
        True 表示代理已启用；False 表示代理测试失败但已禁用并继续运行。

    Raises:
        SystemExit: fail_action='exit' 且代理测试失败时。
    """
    if fail_action not in ("exit", "continue"):
        raise ValueError(f"proxy-fail-action 必须是 'exit' 或 'continue'，当前: {fail_action}")

    config = ProxyConfig(proxy, test_url=test_url, timeout=timeout)
    config.apply()

    if config.test():
        return True

    if fail_action == "exit":
        config.clear()
        logger.error("代理不可用，根据 --proxy-fail-action=exit 结束运行")
        raise SystemExit("代理不可用")

    config.clear()
    logger.warning("代理不可用，根据 --proxy-fail-action=continue 禁用代理并继续运行")
    return False
