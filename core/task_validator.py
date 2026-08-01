"""业务任务存在性校验模块。

导入任务时确认 `business` 包下确实存在指定的模块与函数，
仅做反射检查，不调用业务函数本身。
"""

import importlib
import logging
from typing import Tuple


logger = logging.getLogger("zibllcrawler.core.task_validator")


def validate_business_function(module: str, func: str) -> Tuple[bool, str]:
    """
    验证 `business.{module}.{func}` 是否存在且可调用。

    Returns:
        (是否通过, 错误信息)。通过时错误信息为空字符串。
    """
    full_module = f"business.{module}"
    try:
        mod = importlib.import_module(full_module)
    except ImportError as exc:
        logger.warning(f"业务模块不存在: {full_module}, {exc}")
        return False, f"业务模块不存在: {full_module}"

    try:
        fn = getattr(mod, func)
    except AttributeError:
        logger.warning(f"函数不存在: {full_module}.{func}")
        return False, f"函数不存在: {full_module}.{func}"

    if not callable(fn):
        logger.warning(f"{full_module}.{func} 不是可调用对象")
        return False, f"{full_module}.{func} 不是可调用对象"

    logger.debug(f"业务函数校验通过: {full_module}.{func}")
    return True, ""
