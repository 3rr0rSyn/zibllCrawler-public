"""调度计划计算模块。

根据 `schedules.schedule_type` 与 `schedules.schedule_value` 计算下一次应执行时间 `next_run_at`。

支持的类型：
- now: 立即执行（仅用于一次性启动触发）。
- fixed: 固定时间点，如 08:00，每天同一时刻执行。
- window: 时间窗口，如 08:00-10:00，在窗口内随机选择一次执行时间。
- interval: 固定间隔，如 30 或 00:30，表示每隔多少分钟执行一次。
"""

import logging
import random
from datetime import datetime, timedelta, time
from typing import Optional


logger = logging.getLogger("zibllcrawler.core.schedule_planner")


def format_datetime(dt: datetime) -> str:
    """格式化为与 SQLite CURRENT_TIMESTAMP 一致的字符串。"""
    return dt.replace(microsecond=0).isoformat(sep=" ")


def calculate_next_run_at(
    schedule_type: str,
    schedule_value: Optional[str],
    base_time: Optional[datetime] = None,
    force_tomorrow: bool = False,
) -> Optional[datetime]:
    """
    计算下一次应执行时间。

    Args:
        schedule_type: now / fixed / window / interval。
        schedule_value: 时间字符串，具体格式见模块说明。
        base_time: 计算基准时间，默认当前本地时间。
        force_tomorrow: 是否强制将窗口类型移到明天，用于执行后避免当天重复触发。

    Returns:
        下次执行时间；now 类型返回 base_time；interval 等返回未来时间。
        若 schedule_value 不合法则返回 None。
    """
    now = base_time or datetime.now()

    if schedule_type == "now":
        return now

    if schedule_type == "fixed":
        if not schedule_value:
            return None
        target_time = _parse_time(schedule_value)
        target = datetime.combine(now.date(), target_time)
        if target <= now or force_tomorrow:
            target += timedelta(days=1)
        return target

    if schedule_type == "window":
        if not schedule_value or "-" not in schedule_value:
            return None
        start_str, end_str = schedule_value.split("-", 1)
        start_time = _parse_time(start_str)
        end_time = _parse_time(end_str)

        if force_tomorrow:
            # 执行后统一移到明天窗口
            base_date = (now + timedelta(days=1)).date()
            start_dt = datetime.combine(base_date, start_time)
            end_dt = datetime.combine(base_date, end_time)
        else:
            # 初始化时：今天窗口若未过则用今天，否则明天
            start_dt = datetime.combine(now.date(), start_time)
            end_dt = datetime.combine(now.date(), end_time)
            if end_dt <= now:
                start_dt += timedelta(days=1)
                end_dt += timedelta(days=1)
            elif start_dt <= now < end_dt:
                start_dt = now

        seconds = int((end_dt - start_dt).total_seconds())
        if seconds <= 0:
            return end_dt
        offset = random.randint(0, seconds)
        return start_dt + timedelta(seconds=offset)

    if schedule_type == "interval":
        if not schedule_value:
            return None
        minutes = _parse_interval_minutes(schedule_value)
        if minutes is None or minutes <= 0:
            return None
        return now + timedelta(minutes=minutes)

    logger.warning(f"未知调度类型: {schedule_type}")
    return None


def _parse_time(value: str) -> time:
    """解析时间字符串，支持 08:00、8:00、0800。"""
    value = value.strip()
    if ":" not in value:
        # 0800 -> 08:00
        if len(value) >= 4:
            value = f"{value[:2]}:{value[2:4]}"
        elif len(value) == 3:
            value = f"0{value[0]}:{value[1:3]}"
        else:
            raise ValueError(f"无法解析时间: {value}")
    hour_str, minute_str = value.split(":", 1)
    hour = int(hour_str)
    minute = int(minute_str)
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"时间越界: {value}")
    return time(hour, minute)


def _parse_interval_minutes(value: str) -> Optional[int]:
    """解析间隔分钟数，支持 30、30m、00:30。"""
    value = value.strip().lower()
    if value.endswith("m"):
        return int(value[:-1])
    if ":" in value:
        parts = value.split(":", 1)
        return int(parts[0]) * 60 + int(parts[1])
    return int(value)
