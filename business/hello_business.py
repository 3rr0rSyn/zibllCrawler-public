"""示例业务模块。

用于演示业务函数签名与动态加载机制。
"""

import logging

from typing import Any, Tuple

import requests


logger = logging.getLogger("zibllcrawler.business.hello")


def say_hello(session: requests.Session, name: str = "World") -> Tuple[bool, dict]:
    """
    示例业务函数：返回问候信息。

    Args:
        session: 已登录的 requests.Session 对象（示例中可不使用）。
        name: 问候对象名称。

    Returns:
        (True, {"message": ...})
    """
    msg = f"Hello, {name}!"
    logger.info(msg)
    return True, {"message": msg}
