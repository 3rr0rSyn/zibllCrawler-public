"""签到业务模块。

接收已登录的 `requests.Session`，向目标站点发送签到请求，解析响应并返回结构化结果。
同时支持自检测 `execution_logs`：若主程序传入 `schedule_id` 与 `execution_logger`，
则先检查今天是否已有成功签到记录，有则跳过并向上层返回跳过信息。

不涉及线程、连接池或 session 生命周期管理。
"""

import logging
from typing import Any, Dict, Optional, Tuple

import requests

logger = logging.getLogger("zibllcrawler.business.checkin")


def perform_checkin(session: requests.Session, base_url: str,
                    schedule_id: Optional[int] = None,
                    execution_logger: Optional[Any] = None) -> Tuple[bool, Optional[Dict]]:
    """
    执行签到。

    Args:
        session: 已登录的 requests.Session 对象。
        base_url: 目标站点根 URL（如 https://example.com）。
        schedule_id: 可选，当前调度 ID；传入时用于检测本日是否已执行过。
        execution_logger: 可选，执行日志记录器；需与 schedule_id 同时传入。

    Returns:
        (是否成功, 结果数据):
        - 本日已执行，跳过: (True, {"skipped": True, "msg": "..."})
        - 新签到成功: (True, {"points": ..., "integral": ..., ...})
        - 站点返回今日已签到: (False, {"msg": "今日已签到", ...})
        - 其他失败: (False, {"msg": "错误信息"}) 或 (False, None)
    """
    logger.info(f"开始签到: {base_url}")

    # 自检测：若今天已有成功执行记录，则跳过，避免重复请求站点
    if schedule_id and execution_logger and execution_logger.has_success_today(schedule_id):
        msg = "本日已存在成功签到记录，跳过任务"
        logger.info(msg)
        return True, {"skipped": True, "msg": msg}

    checkin_url = f"{base_url}/wp-admin/admin-ajax.php"

    try:
        resp = session.post(
            checkin_url,
            data={"action": "user_checkin"},
            timeout=10
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error(f"签到请求失败: {exc}")
        return False, None

    # 成功签到
    if data.get("error") is False:
        payload = data.get("data", {})
        result = {
            "points": payload.get("points"),
            "integral": payload.get("integral"),
            "time": payload.get("time"),
            "continuous_day": data.get("continuous_day"),
            "msg": data.get("msg"),
        }
        logger.info(f"签到成功: {result['msg']}")
        return True, result

    # 站点返回今日已签到（不视为错误，但无奖励）
    msg = data.get("msg", "")
    if "今日已签到" in msg:
        logger.info(f"已签到: {msg}")
        return False, {"msg": "今日已签到", "full_msg": msg}

    # 其他失败
    logger.error(f"签到失败: {msg or '未知错误'}")
    return False, {"msg": msg or "未知错误"}
