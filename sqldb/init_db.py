#!/usr/bin/env python3
"""初始化 zibllCrawler SQLite 数据库。

根据 `AGENTS.md` 与 `项目部分设计.md` 中的 6 张表设计，适配 SQLite 方言后创建：
- websites
- accounts
- site_accounts
- tasks
- schedules
- execution_logs

同时建立常用索引、外键约束、ON UPDATE CURRENT_TIMESTAMP 触发器。
"""

import sqlite3
from pathlib import Path

# 数据库文件路径
DB_PATH = Path(__file__).parent / "zibllcrawler.db"

CREATE_TABLES_SQL = """
-- 启用外键约束（SQLite 默认关闭）
PRAGMA foreign_keys = ON;

-- 1. 网站表
CREATE TABLE IF NOT EXISTS websites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    aliases TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. 账号表
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    password TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(username, password)
);

-- 3. 网站账号关联表
CREATE TABLE IF NOT EXISTS site_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    cookie TEXT,
    login_adapter TEXT NOT NULL DEFAULT 'zibll_slider',
    is_enabled INTEGER NOT NULL DEFAULT 0 CHECK(is_enabled IN (0, 1)),
    last_used_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(site_id, account_id),
    FOREIGN KEY (site_id) REFERENCES websites(id) ON DELETE CASCADE,
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE
);

-- 4. 任务表
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_name TEXT NOT NULL UNIQUE,
    module TEXT NOT NULL,
    func TEXT NOT NULL,
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(module, func)
);

-- 5. 调度表
CREATE TABLE IF NOT EXISTS schedules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_account_id INTEGER NOT NULL,
    task_id INTEGER NOT NULL,
    schedule_type TEXT NOT NULL DEFAULT 'now' CHECK(schedule_type IN ('now', 'fixed', 'window', 'interval')),
    schedule_value TEXT,
    is_enabled INTEGER NOT NULL DEFAULT 1 CHECK(is_enabled IN (0, 1)),
    last_run_at DATETIME,
    next_run_at DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(site_account_id, task_id),
    FOREIGN KEY (site_account_id) REFERENCES site_accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
);

-- 6. 执行日志表
CREATE TABLE IF NOT EXISTS execution_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    schedule_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'success', 'failed')),
    result_message TEXT,
    started_at DATETIME NOT NULL,
    finished_at DATETIME,
    duration_ms INTEGER,
    FOREIGN KEY (schedule_id) REFERENCES schedules(id) ON DELETE CASCADE
);
"""

CREATE_INDEXES_SQL = """
CREATE INDEX IF NOT EXISTS idx_site_accounts_site_id ON site_accounts(site_id);
CREATE INDEX IF NOT EXISTS idx_site_accounts_account_id ON site_accounts(account_id);
CREATE INDEX IF NOT EXISTS idx_schedules_site_account_id ON schedules(site_account_id);
CREATE INDEX IF NOT EXISTS idx_schedules_task_id ON schedules(task_id);
CREATE INDEX IF NOT EXISTS idx_schedules_next_run_at ON schedules(next_run_at);
CREATE INDEX IF NOT EXISTS idx_execution_logs_schedule_id ON execution_logs(schedule_id);
CREATE INDEX IF NOT EXISTS idx_execution_logs_status ON execution_logs(status);
CREATE INDEX IF NOT EXISTS idx_execution_logs_started_at ON execution_logs(started_at);
"""

CREATE_TRIGGERS_SQL = """
-- SQLite 不支持 ON UPDATE CURRENT_TIMESTAMP，用触发器模拟
CREATE TRIGGER IF NOT EXISTS trg_site_accounts_updated_at
AFTER UPDATE ON site_accounts
FOR EACH ROW
BEGIN
    UPDATE site_accounts
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_schedules_updated_at
AFTER UPDATE ON schedules
FOR EACH ROW
BEGIN
    UPDATE schedules
    SET updated_at = CURRENT_TIMESTAMP
    WHERE id = NEW.id;
END;
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """若表中没有指定列，则执行 ALTER TABLE 添加。"""
    cursor = conn.execute(f"PRAGMA table_info({table})")
    exists = any(row[1] == column for row in cursor.fetchall())
    if not exists:
        conn.execute(ddl)


def init_db() -> None:
    """创建数据库及表结构。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(CREATE_TABLES_SQL)
        _ensure_column(
            conn,
            "websites",
            "aliases",
            "ALTER TABLE websites ADD COLUMN aliases TEXT",
        )
        conn.executescript(CREATE_INDEXES_SQL)
        conn.executescript(CREATE_TRIGGERS_SQL)
        conn.commit()
        print(f"数据库初始化完成：{DB_PATH}")
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
