"""SQLite 数据库连接池。

连接按线程隔离：每个工作线程首次请求连接时创建一条 sqlite3 连接，
后续复用同一条连接。线程退出时由线程池统一管理，程序结束时需显式
调用 close_all() 关闭所有连接。
"""

import logging
import os
import sqlite3
import threading
from pathlib import Path
from typing import Optional


class SQLiteConnectionPool:
    """SQLite 线程隔离连接池。"""

    def __init__(self, db_path: str | Path, max_connections: Optional[int] = None):
        self.logger = logging.getLogger("zibllcrawler.core.db_pool")
        self.db_path = Path(db_path)
        self.max_connections = max_connections or (os.cpu_count() or 4) * 2
        self._local = threading.local()
        self._all_connections: list[sqlite3.Connection] = []
        self._lock = threading.Lock()
        self.logger.info(f"初始化 SQLite 连接池，数据库={self.db_path}")

    def _create_connection(self) -> sqlite3.Connection:
        """创建并配置一条新连接。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.row_factory = sqlite3.Row
        with self._lock:
            self._all_connections.append(conn)
        self.logger.debug(f"创建新连接，当前总连接数={len(self._all_connections)}")
        return conn

    def get_connection(self) -> sqlite3.Connection:
        """获取当前线程对应的连接；不存在则创建。"""
        conn = getattr(self._local, "connection", None)
        if conn is None:
            conn = self._create_connection()
            self._local.connection = conn
        return conn

    def close_all(self) -> None:
        """关闭池内所有连接。"""
        with self._lock:
            connections = self._all_connections[:]
            self._all_connections.clear()
        for conn in connections:
            try:
                conn.close()
            except Exception as e:
                self.logger.warning(f"关闭连接时出错: {e}")
        self._local.connection = None
        self.logger.info("SQLite 连接池已关闭")
