"""登录适配器工厂。

根据 `site_accounts.login_adapter` 字段名称创建对应的登录适配器实例。
新增适配器时在此注册即可。
"""

import logging
from typing import Optional

import requests

from adapters.base_login import BaseLoginAdapter, ZibllSliderLoginAdapter
from adapters.captcha_solver import SliderCaptchaSolver
from core.db_pool import SQLiteConnectionPool


logger = logging.getLogger("zibllcrawler.adapters.factory")

# 适配器名称 -> 适配器类
_ADAPTER_REGISTRY = {
    "zibll_slider": ZibllSliderLoginAdapter,
}


def create_login_adapter(name: str, db_pool: Optional[SQLiteConnectionPool] = None) -> BaseLoginAdapter:
    """
    根据名称创建登录适配器。

    Args:
        name: `site_accounts.login_adapter` 字段值。
        db_pool: 可选的数据库连接池，用于持久化 Cookie。

    Returns:
        配置好的登录适配器实例。

    Raises:
        ValueError: 未注册的适配器名称。
    """
    adapter_cls = _ADAPTER_REGISTRY.get(name)
    if adapter_cls is None:
        raise ValueError(f"未知的登录适配器: {name}，已注册: {list(_ADAPTER_REGISTRY.keys())}")

    captcha_solver = SliderCaptchaSolver()
    adapter = adapter_cls(captcha_solver, db_pool=db_pool)
    logger.debug(f"创建登录适配器: {name}")
    return adapter


def supported_adapters() -> list[str]:
    """返回已注册的适配器名称列表。"""
    return list(_ADAPTER_REGISTRY.keys())


def login_with_adapter(adapter: BaseLoginAdapter, site_account_id: int, base_url: str,
                       username: str, password: str, cookie: Optional[str] = None) -> Optional[requests.Session]:
    """
    统一调用适配器的登录方法。

    简化调用方代码，同时保持适配器接口一致。
    """
    return adapter.login(site_account_id, base_url, username, password, cookie)
