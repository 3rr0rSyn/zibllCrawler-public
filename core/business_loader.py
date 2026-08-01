"""业务模块动态加载器。

根据模块名与函数名从 `business` 包中动态导入并执行，
自动将 `session` 作为第一个参数传入业务函数。
"""

import importlib
import logging
from typing import Any

import requests


logger = logging.getLogger("zibllcrawler.core.business_loader")


def run_business(session: requests.Session, module_name: str, func_name: str, **kwargs) -> Any:
    """
    动态加载并执行业务函数。

    Args:
        session: 已登录的 requests.Session，作为业务函数第一个参数。
        module_name: 业务模块名，如 "checkin_business"。
        func_name: 函数名，如 "perform_checkin"。
        **kwargs: 业务函数的其他参数。

    Returns:
        业务函数返回值。
    """
    logger.info(f"执行业务: business.{module_name}.{func_name}")
    try:
        module = importlib.import_module(f"business.{module_name}")
    except ImportError as exc:
        raise RuntimeError(f"业务模块不存在: business.{module_name}") from exc

    try:
        func = getattr(module, func_name)
    except AttributeError as exc:
        raise RuntimeError(f"业务函数不存在: {func_name}") from exc

    return func(session, **kwargs)
