"""执行日志记录器。

将任务执行结果写入 `execution_logs` 表。
表结构：`schedule_id`, `status`, `result_message`, `started_at`, `finished_at`, `duration_ms`。
"""

import logging
from typing import Optional

from core.db_pool import SQLiteConnectionPool


class ExecutionLogger:
    """任务执行日志记录器。"""

    def __init__(self, db_pool: SQLiteConnectionPool):
        self.db_pool = db_pool
        self.logger = logging.getLogger("zibllcrawler.core.execution_logger")

    def has_success_today(self, schedule_id: int) -> bool:
        """
        检查指定 schedule_id 今天是否已有成功执行记录。
        通过 started_at 字段的前缀匹配当前日期实现，避免依赖 SQLite 日期函数解析格式。
        """
        from datetime import date
        today = date.today().isoformat()  # YYYY-MM-DD
        conn = self.db_pool.get_connection()
        cursor = conn.execute(
            """
            SELECT 1 FROM execution_logs
            WHERE schedule_id = ? AND status = 'success' AND started_at LIKE ?
            LIMIT 1
            """,
            (schedule_id, f"{today}%")
        )
        exists = cursor.fetchone() is not None
        self.logger.debug(
            f"schedule_id={schedule_id} 今天{'已有' if exists else '尚无'}成功执行记录"
        )
        return exists

    def log(self, schedule_id: int, status: str, result_message: Optional[str] = None,
            started_at: Optional[str] = None, finished_at: Optional[str] = None,
            duration_ms: Optional[int] = None) -> None:
        """写入一条执行日志记录。"""
        conn = self.db_pool.get_connection()
        conn.execute(
            """
            INSERT INTO execution_logs
            (schedule_id, status, result_message, started_at, finished_at, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (schedule_id, status, result_message, started_at, finished_at, duration_ms)
        )
        conn.commit()
        self.logger.debug(f"已记录执行日志 schedule_id={schedule_id}, status={status}")

