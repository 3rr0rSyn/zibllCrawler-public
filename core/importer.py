"""手动数据导入模块。

支持通过命令行参数进入交互式菜单或非交互式导入，完成：
- 导入网站（含别名、适配性检测、可选测试账号）
- 导入任务（校验 business 模块/函数存在性）
- 导入账号（绑定已有网站与任务，登录测试通过后落库）

导入完成后直接退出，不进入主调度循环。
"""

import logging
import os
import sys
import threading
import time
from argparse import Namespace
from typing import List, Optional, Tuple

import requests

from adapters.captcha_solver import SliderCaptchaSolver
from adapters.factory import create_login_adapter
from adapters.site_detector import SiteDetector
from core.db_pool import SQLiteConnectionPool
from core.password_crypto import PasswordCrypto
from core.schedule_planner import calculate_next_run_at, format_datetime
from core.task_validator import validate_business_function
from core.validators import (
    ValidationError,
    parse_aliases,
    sanitize_identifier,
    sanitize_password,
    sanitize_url,
    sanitize_username,
    serialize_aliases,
)


logger = logging.getLogger("zibllcrawler.core.importer")

DEFAULT_ADAPTER = "zibll_slider"


class Importer:
    """数据导入器：网站、任务、账号。"""

    def __init__(
        self,
        db_pool: SQLiteConnectionPool,
        crypto: PasswordCrypto,
        interactive_timeout: int = 300,
    ):
        self.db_pool = db_pool
        self.crypto = crypto
        self.interactive_timeout = interactive_timeout
        self.detector = SiteDetector(captcha_solver=SliderCaptchaSolver())

    # ------------------------------------------------------------------
    # 公共入口
    # ------------------------------------------------------------------

    def run(self, args: Namespace) -> None:
        """根据命令行参数进入对应导入流程。"""
        if args.import_site:
            self.import_site_noninteractive(args)
        elif args.import_task:
            self.import_task_noninteractive(args)
        elif args.import_account:
            self.import_account_noninteractive(args)
        elif args.interactive_import:
            self._interactive_loop()
        else:
            logger.error("导入模式已启用但未指定具体导入类型")

    def _interactive_loop(self) -> None:
        """交互式导入菜单。"""
        self._start_timeout_timer()
        print("\n=== zibllCrawler 数据导入 ===")
        print("支持导入：1.网站  2.任务  3.账号  q.退出")

        while True:
            print()
            choice = self._input_with_timeout("请选择: ").strip().lower()
            if choice in ("q", "quit", "exit"):
                print("退出导入")
                break
            if choice == "1":
                self.import_site_interactive()
            elif choice == "2":
                self.import_task_interactive()
            elif choice == "3":
                self.import_account_interactive()
            else:
                print("无效选项")

    # ------------------------------------------------------------------
    # 网站导入
    # ------------------------------------------------------------------

    def import_site_interactive(self) -> None:
        """交互式导入网站。"""
        print("\n--- 导入网站 ---")
        raw_url = self._input_with_timeout("请输入网站 URL: ").strip()
        try:
            url = sanitize_url(raw_url)
        except ValidationError as exc:
            print(f"URL 校验失败: {exc.message}")
            return

        existing = self._find_website(url)
        if existing:
            print(f"网站已存在: {existing['name']} ({existing['url']})")
            return

        name = self._input_with_timeout("请输入站点名称（留空使用域名）: ").strip()
        if not name:
            from urllib.parse import urlparse
            name = urlparse(url).netloc or url

        raw_aliases = self._input_with_timeout("请输入别名 URL（多个用逗号分隔，留空跳过）: ").strip()
        try:
            aliases = parse_aliases(raw_aliases)
        except ValidationError as exc:
            print(f"别名校验失败: {exc.message}")
            return

        test_username = self._input_with_timeout("请输入测试账号（留空使用默认测试账号）: ").strip()
        test_password = ""
        if test_username:
            try:
                test_username = sanitize_username(test_username)
                test_password = self._input_with_timeout("请输入测试密码: ")
                test_password = sanitize_password(test_password)
            except ValidationError as exc:
                print(f"测试账号校验失败: {exc.message}")
                return
        else:
            test_username = None
            test_password = None

        ok, message = self._do_import_site(
            name=name,
            url=url,
            aliases=aliases,
            test_username=test_username,
            test_password=test_password,
        )
        print(message)

    def import_site_noninteractive(self, args: Namespace) -> None:
        """非交互式导入网站。"""
        try:
            url = sanitize_url(args.site_url)
            name = args.site_name.strip() or url
            aliases = parse_aliases(args.site_aliases) if args.site_aliases else []
            test_username = sanitize_username(args.site_test_username) if args.site_test_username else None
            test_password = (
                sanitize_password(args.site_test_password)
                if args.site_test_password
                else None
            )
        except ValidationError as exc:
            logger.error(f"参数校验失败: {exc}")
            return

        existing = self._find_website(url)
        if existing:
            logger.warning(f"网站已存在: {existing['url']}")
            return

        ok, message = self._do_import_site(
            name=name,
            url=url,
            aliases=aliases,
            test_username=test_username,
            test_password=test_password,
        )
        logger.info(message)

    def _do_import_site(
        self,
        name: str,
        url: str,
        aliases: List[str],
        test_username: Optional[str],
        test_password: Optional[str],
    ) -> Tuple[bool, str]:
        """执行网站导入核心逻辑。"""
        # 主域名检测
        main_result = self.detector.detect(url, test_username=test_username, test_password=test_password)
        if not main_result["adaptable"]:
            return False, f"网站检测失败: {main_result['reason']}"

        # 别名检测
        for alias in aliases:
            alias_result = self.detector.verify_alias(
                url, alias, test_username=test_username, test_password=test_password
            )
            if not alias_result["adaptable"]:
                return False, f"别名检测失败 [{alias}]: {alias_result['reason']}"

        # 写入 websites
        conn = self.db_pool.get_connection()
        aliases_json = serialize_aliases(aliases)
        conn.execute(
            "INSERT INTO websites (name, url, aliases) VALUES (?, ?, ?)",
            (name, url, aliases_json),
        )
        conn.commit()
        site_id = conn.execute("SELECT id FROM websites WHERE url = ?", (url,)).fetchone()[0]
        logger.info(f"已导入网站: {name} ({url}), id={site_id}")

        # 如果提供了测试账号且检测成功，顺便创建账号与 site_accounts
        if test_username and test_password:
            self._create_account_and_site_account(
                site_id=site_id,
                username=test_username,
                password=test_password,
                adapter=DEFAULT_ADAPTER,
                enabled=True,
            )
            return True, f"网站导入成功，并创建测试账号关联: {url}"

        return True, f"网站导入成功: {url}"

    # ------------------------------------------------------------------
    # 任务导入
    # ------------------------------------------------------------------

    def import_task_interactive(self) -> None:
        """交互式导入任务。"""
        print("\n--- 导入任务 ---")
        try:
            task_name = sanitize_identifier(self._input_with_timeout("TASK_NAME: "), "task_name")
            module = sanitize_identifier(self._input_with_timeout("MODULE: "), "module")
            func = sanitize_identifier(self._input_with_timeout("FUNC: "), "func")
        except ValidationError as exc:
            print(f"输入校验失败: {exc.message}")
            return

        description = self._input_with_timeout("DESCRIPTION（留空跳过）: ").strip()

        ok, msg = self._do_import_task(task_name, module, func, description)
        print(msg)

    def import_task_noninteractive(self, args: Namespace) -> None:
        """非交互式导入任务。"""
        try:
            task_name = sanitize_identifier(args.task_name, "task_name")
            module = sanitize_identifier(args.task_module, "module")
            func = sanitize_identifier(args.task_func, "func")
        except ValidationError as exc:
            logger.error(f"参数校验失败: {exc}")
            return
        description = (args.task_desc or "").strip()

        ok, msg = self._do_import_task(task_name, module, func, description)
        logger.info(msg)

    def _do_import_task(
        self, task_name: str, module: str, func: str, description: str
    ) -> Tuple[bool, str]:
        ok, error = validate_business_function(module, func)
        if not ok:
            return False, f"任务校验失败: {error}"

        conn = self.db_pool.get_connection()
        try:
            conn.execute(
                "INSERT INTO tasks (task_name, module, func, description) VALUES (?, ?, ?, ?)",
                (task_name, module, func, description),
            )
            conn.commit()
            return True, f"任务导入成功: {task_name} -> business.{module}.{func}"
        except Exception as exc:
            return False, f"任务写入数据库失败: {exc}"

    # ------------------------------------------------------------------
    # 账号导入
    # ------------------------------------------------------------------

    def import_account_interactive(self) -> None:
        """交互式导入账号，支持仅使用 Cookie 登录。"""
        print("\n--- 导入账号 ---")
        try:
            username = sanitize_username(self._input_with_timeout("用户名: "))
        except ValidationError as exc:
            print(f"输入校验失败: {exc.message}")
            return

        password = self._input_with_timeout("密码（若使用 Cookie 登录可留空）: ").strip()
        if password:
            try:
                password = sanitize_password(password)
            except ValidationError as exc:
                print(f"输入校验失败: {exc.message}")
                return
        else:
            password = ""

        try:
            cookie = self._sanitize_cookie(
                self._input_with_timeout("Cookie（从浏览器复制，留空表示使用密码登录）: ")
            )
        except ValidationError as exc:
            print(f"输入校验失败: {exc.message}")
            return

        if not password and not cookie:
            print("密码和 Cookie 不能同时为空")
            return

        site_raw = self._input_with_timeout("绑定网站 URL 或域名: ").strip()
        try:
            site_url = sanitize_url(site_raw)
        except ValidationError as exc:
            print(f"网站 URL 校验失败: {exc.message}")
            return

        site = self._find_website(site_url)
        if not site:
            print(f"网站不存在: {site_url}")
            return

        # 优先校验 Cookie
        if cookie:
            cookie_ok, cookie_msg = self._validate_cookie(site["url"], username, cookie)
            if cookie_ok:
                print(f"Cookie 有效: {cookie_msg}")
            else:
                print(f"Cookie 无效: {cookie_msg}")
                if not password:
                    print("未提供密码且 Cookie 无效，无法导入")
                    return
                print("Cookie 无效，将使用密码登录测试")

        # 密码登录测试
        if password:
            login_ok, login_msg = self._test_login(site["url"], username, password)
            if not login_ok:
                print(f"登录测试失败: {login_msg}")
                return
            print(f"登录测试通过: {login_msg}")

        # 列出任务
        tasks = self._list_tasks()
        if not tasks:
            print("当前没有可用任务，请先导入任务")
            return

        print("可用任务:")
        for idx, task in enumerate(tasks, 1):
            print(f"  {idx}. {task['task_name']} ({task['module']}.{task['func']})")

        task_idx_str = self._input_with_timeout("请选择任务编号: ").strip()
        try:
            task_idx = int(task_idx_str) - 1
            if task_idx < 0 or task_idx >= len(tasks):
                raise ValueError
        except ValueError:
            print("无效的任务编号")
            return
        task = tasks[task_idx]

        schedule_type = self._input_with_timeout("调度类型 [now/fixed/window/interval]（默认 now）: ").strip()
        if not schedule_type:
            schedule_type = "now"
        schedule_value = self._input_with_timeout("调度值（留空表示 now）: ").strip() or None

        ok, msg = self._do_import_account(
            username=username,
            password=password,
            site_id=site["id"],
            task_id=task["id"],
            cookie=cookie or None,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
        )
        print(msg)

    def import_account_noninteractive(self, args: Namespace) -> None:
        """非交互式导入账号，支持仅使用 Cookie 登录。"""
        try:
            username = sanitize_username(args.account_username)
            password = sanitize_password(args.account_password) if args.account_password else ""
            site_url = sanitize_url(args.account_site)
        except ValidationError as exc:
            logger.error(f"参数校验失败: {exc}")
            return

        try:
            cookie = self._sanitize_cookie(args.account_cookie or "")
        except ValidationError as exc:
            logger.error(f"参数校验失败: {exc}")
            return
        if not password and not cookie:
            logger.error("密码和 Cookie 不能同时为空")
            return

        site = self._find_website(site_url)
        if not site:
            logger.error(f"网站不存在: {site_url}")
            return

        # 优先校验 Cookie
        if cookie:
            cookie_ok, cookie_msg = self._validate_cookie(site["url"], username, cookie)
            if cookie_ok:
                logger.info(f"Cookie 有效: {cookie_msg}")
            else:
                if not password:
                    logger.error(f"Cookie 无效且未提供密码: {cookie_msg}")
                    return
                logger.warning(f"Cookie 无效，将使用密码登录测试: {cookie_msg}")

        # 密码登录测试
        if password:
            login_ok, login_msg = self._test_login(site["url"], username, password)
            if not login_ok:
                logger.error(f"登录测试失败: {login_msg}")
                return
            logger.info(f"登录测试通过: {login_msg}")

        task = self._find_task(args.account_task)
        if not task:
            logger.error(f"任务不存在: {args.account_task}")
            return

        schedule_type = args.schedule_type or "now"
        schedule_value = args.schedule_value or None

        ok, msg = self._do_import_account(
            username=username,
            password=password,
            site_id=site["id"],
            task_id=task["id"],
            cookie=cookie or None,
            schedule_type=schedule_type,
            schedule_value=schedule_value,
        )
        logger.info(msg)

    def _do_import_account(
        self,
        username: str,
        password: str,
        site_id: int,
        task_id: int,
        cookie: Optional[str] = None,
        schedule_type: str = "now",
        schedule_value: Optional[str] = None,
    ) -> Tuple[bool, str]:
        """执行账号导入核心逻辑。"""
        encrypted_password = self.crypto.encrypt(password)
        conn = self.db_pool.get_connection()

        try:
            # 写入或复用 accounts
            conn.execute(
                "INSERT OR IGNORE INTO accounts (username, password) VALUES (?, ?)",
                (username, encrypted_password),
            )
            account_row = conn.execute(
                "SELECT id FROM accounts WHERE username = ? AND password = ?",
                (username, encrypted_password),
            ).fetchone()
            if account_row is None:
                # 相同用户名但不同密码时，回退到按用户名匹配，避免导入中断
                account_row = conn.execute(
                    "SELECT id FROM accounts WHERE username = ?",
                    (username,),
                ).fetchone()
            account_id = account_row[0]

            # site_accounts
            conn.execute(
                """
                INSERT OR IGNORE INTO site_accounts
                (site_id, account_id, cookie, login_adapter, is_enabled)
                VALUES (?, ?, ?, ?, ?)
                """,
                (site_id, account_id, cookie, DEFAULT_ADAPTER, 1),
            )
            site_account_id = conn.execute(
                "SELECT id FROM site_accounts WHERE site_id = ? AND account_id = ?",
                (site_id, account_id),
            ).fetchone()[0]

            # schedules
            next_run_at = None
            if schedule_type != "now":
                next_run_at = calculate_next_run_at(schedule_type, schedule_value)

            conn.execute(
                """
                INSERT OR IGNORE INTO schedules
                (site_account_id, task_id, schedule_type, schedule_value, next_run_at, is_enabled)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    site_account_id,
                    task_id,
                    schedule_type,
                    schedule_value,
                    format_datetime(next_run_at) if next_run_at else None,
                    1,
                ),
            )
            conn.commit()
            return True, f"账号导入并绑定成功: {username} -> site_id={site_id}, task_id={task_id}"
        except Exception as exc:
            return False, f"账号导入失败: {exc}"

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _find_website(self, url: str) -> Optional[dict]:
        """根据 URL 或别名查找网站。"""
        conn = self.db_pool.get_connection()
        cursor = conn.execute("SELECT id, name, url, aliases FROM websites WHERE url = ?", (url,))
        row = cursor.fetchone()
        if row:
            return dict(row)

        # 查别名
        cursor = conn.execute("SELECT id, name, url, aliases FROM websites")
        for row in cursor.fetchall():
            aliases = []
            if row["aliases"]:
                try:
                    from core.validators import parse_json_aliases
                    aliases = parse_json_aliases(row["aliases"])
                except Exception:
                    continue
            if url in aliases:
                return dict(row)
        return None

    def _list_tasks(self) -> List[dict]:
        """列出所有任务。"""
        conn = self.db_pool.get_connection()
        cursor = conn.execute("SELECT id, task_name, module, func FROM tasks ORDER BY id")
        return [dict(row) for row in cursor.fetchall()]

    def _find_task(self, task_name: str) -> Optional[dict]:
        """根据任务名查找任务。"""
        conn = self.db_pool.get_connection()
        cursor = conn.execute(
            "SELECT id, task_name, module, func FROM tasks WHERE task_name = ?",
            (task_name,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None

    def _test_login(
        self, base_url: str, username: str, password: str
    ) -> Tuple[bool, str]:
        """使用通用适配器测试登录。"""
        try:
            adapter = create_login_adapter(DEFAULT_ADAPTER, db_pool=None)
            session = adapter.login(
                site_account_id=-1,
                base_url=base_url,
                username=username,
                password=password,
                cookie=None,
            )
            if session is None:
                return False, "适配器返回登录失败"
            return True, f"登录成功: {base_url}"
        except Exception as exc:
            return False, f"登录测试异常: {exc}"

    @staticmethod
    def _sanitize_cookie(value: str) -> Optional[str]:
        """对 Cookie 字符串做轻量校验，保留浏览器 Cookie 原格式。"""
        value = value.strip()
        if not value:
            return None
        if len(value) > 8192:
            raise ValidationError("cookie", "Cookie 长度不能超过 8192 字符")
        if "\x00" in value:
            raise ValidationError("cookie", "Cookie 包含非法空字符")
        return value

    def _validate_cookie(
        self, base_url: str, username: str, cookie: str
    ) -> Tuple[bool, str]:
        """直接探测 Cookie 是否仍有效，并确认登录用户与预期一致。"""
        session = requests.Session()
        session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": f"{base_url}/",
            "X-Requested-With": "XMLHttpRequest",
        })

        for part in cookie.split(";"):
            part = part.strip()
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key and value:
                session.cookies.set(key, value)

        try:
            resp = session.post(
                f"{base_url}/wp-admin/admin-ajax.php",
                data={"action": "get_current_user"},
                timeout=10,
            )
            if resp.status_code != 200:
                return False, f"探测接口返回非 200: {resp.status_code}"
            data = resp.json()
            if data.get("is_logged_in") is False:
                return False, "Cookie 明确未登录"
            if data.get("id", 0) <= 0 or not data.get("user_data"):
                return False, "Cookie 未处于登录状态"
            user_login = (data.get("user_data") or {}).get("user_login")
            if user_login != username:
                return False, f"Cookie 对应账号为 '{user_login}'，与 '{username}' 不一致"
            return True, f"已登录用户: {user_login}"
        except Exception as exc:
            return False, f"Cookie 探测异常: {exc}"

    def _create_account_and_site_account(
        self,
        site_id: int,
        username: str,
        password: str,
        adapter: str,
        enabled: bool,
    ) -> int:
        """创建账号并关联到网站，返回 site_account_id。"""
        encrypted_password = self.crypto.encrypt(password)
        conn = self.db_pool.get_connection()

        conn.execute(
            "INSERT OR IGNORE INTO accounts (username, password) VALUES (?, ?)",
            (username, encrypted_password),
        )
        account_id = conn.execute(
            "SELECT id FROM accounts WHERE username = ? AND password = ?",
            (username, encrypted_password),
        ).fetchone()[0]

        conn.execute(
            """
            INSERT OR IGNORE INTO site_accounts
            (site_id, account_id, cookie, login_adapter, is_enabled)
            VALUES (?, ?, ?, ?, ?)
            """,
            (site_id, account_id, None, adapter, 1 if enabled else 0),
        )
        site_account_id = conn.execute(
            "SELECT id FROM site_accounts WHERE site_id = ? AND account_id = ?",
            (site_id, account_id),
        ).fetchone()[0]
        conn.commit()
        return site_account_id

    # ------------------------------------------------------------------
    # 交互超时控制
    # ------------------------------------------------------------------

    def _start_timeout_timer(self) -> None:
        """启动导入模式超时退出定时器。"""
        if self.interactive_timeout <= 0:
            return

        def _timeout_exit():
            time.sleep(self.interactive_timeout)
            print(f"\n导入模式超过 {self.interactive_timeout} 秒未操作，自动退出")
            os._exit(0)

        timer = threading.Thread(target=_timeout_exit, daemon=True)
        timer.start()

    def _input_with_timeout(self, prompt: str) -> str:
        """带超时提示的 input（实际超时由后台守护线程处理）。"""
        try:
            return input(prompt)
        except EOFError:
            print("输入流已结束，退出导入")
            sys.exit(0)
