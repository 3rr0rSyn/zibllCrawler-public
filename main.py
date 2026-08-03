"""zibllCrawler 主程序入口。

职责：
1. 启动时调用环境检测与初始化模块，确认运行环境满足最小条件。
2. 初始化全局日志、SQLite 连接池、全局线程池、执行日志记录器、密码解密器、代理配置。
3. 支持命令行参数：
   - --detect：批量检测站点适配性，完成后退出。
   - --proxy：设置全局代理，并可选择代理失败时退出或继续。
   - --max-loops：限制调度循环次数（用于测试），默认 0 表示无限循环。
4. 启动时立即执行所有 `now` 类型调度（执行后自动禁用）。
5. 进入循环调度模式，每 60 秒扫描一次 `fixed` / `window` / `interval` 类型调度，
   到达 `next_run_at` 时提交到线程池执行，并更新下次执行时间。
6. 使用 `core.password_crypto.PasswordCrypto` 解密 `accounts.password`。
7. 根据 `site_accounts.login_adapter` 字段选择对应的登录适配器。
8. 登录成功后通过业务加载器执行业务函数。
9. 将执行结果（成功/失败、耗时等）写入 `execution_logs` 表。
10. 关闭线程池与连接池。
"""

import argparse
import sys
import time
from datetime import datetime

import requests

from logger_setup import setup_logger
from core.db_pool import SQLiteConnectionPool
from core.scheduler import ThreadPoolScheduler
from core.business_loader import run_business
from core.execution_logger import ExecutionLogger
from core.importer import Importer
from core.password_crypto import PasswordCrypto
from core.proxy_config import configure_proxy
from core.schedule_planner import calculate_next_run_at, format_datetime
from core.validators import parse_json_aliases
from adapters.factory import create_login_adapter
from adapters.site_detector import detect_sites


SQL_SCHEDULES = """
SELECT
    s.id AS schedule_id,
    sa.id AS site_account_id,
    w.url AS base_url,
    w.aliases AS aliases,
    a.username,
    a.password,
    sa.cookie,
    sa.login_adapter,
    t.module AS task_module,
    t.func AS task_func,
    s.schedule_type,
    s.schedule_value,
    s.next_run_at
FROM schedules s
JOIN site_accounts sa ON s.site_account_id = sa.id
JOIN websites w ON sa.site_id = w.id
JOIN accounts a ON sa.account_id = a.id
JOIN tasks t ON s.task_id = t.id
WHERE s.is_enabled = 1 AND (s.next_run_at IS NULL OR s.next_run_at <= ?)
ORDER BY s.id
"""


def execute_schedule(schedule: tuple, db_pool: SQLiteConnectionPool,
                     execution_logger: ExecutionLogger, crypto: PasswordCrypto) -> None:
    """
    执行单个调度：登录 + 运行业务函数 + 记录执行日志。

    若主域名无法通信，会依次尝试 `websites.aliases` 中的别名 URL。
    明确登录失败（如账号密码错误）时不再尝试别名。

    Args:
        schedule: 数据库查询返回的调度元组。
        db_pool: SQLite 连接池。
        execution_logger: 执行日志记录器。
        crypto: 密码解密器。
    """
    logger = setup_logger()
    (
        schedule_id,
        site_account_id,
        base_url,
        aliases_json,
        username,
        encrypted_password,
        cookie,
        login_adapter,
        task_module,
        task_func,
        schedule_type,
        schedule_value,
        next_run_at,
    ) = schedule

    started_at = datetime.now()
    status = "failed"
    result_message = "未知错误"

    try:
        aliases = parse_json_aliases(aliases_json)
    except Exception:
        aliases = []

    try:
        password = crypto.decrypt(encrypted_password)
        logger.info(f"执行调度 {schedule_id}: {base_url}, 用户 {username}, 适配器 {login_adapter}")
        adapter = create_login_adapter(login_adapter, db_pool=db_pool)

        # 先尝试主域名；若通信失败则尝试别名
        session, execution_url = _try_login_with_aliases(
            adapter=adapter,
            site_account_id=site_account_id,
            base_url=base_url,
            aliases=aliases,
            username=username,
            password=password,
            cookie=cookie,
            schedule_id=schedule_id,
        )

        if session is None:
            status = "failed"
            if not password:
                result_message = "登录失败：账号未设置密码且 Cookie 失效，已停用该账号"
                _disable_site_account(site_account_id, db_pool)
                logger.warning(f"site_account_id={site_account_id} 无密码且 Cookie 失效，已停用")
            else:
                result_message = "登录失败"
        else:
            success, data = run_business(
                session,
                task_module,
                task_func,
                base_url=execution_url,
                schedule_id=schedule_id,
                execution_logger=execution_logger,
            )

            if success and data and data.get("skipped"):
                # 业务层自检测发现本日已执行，跳过
                status = "success"
                result_message = f"任务跳过: {data.get('msg')}"
                logger.info(f"调度 {schedule_id} 跳过: {data.get('msg')}")
            elif success:
                status = "success"
                result_message = f"任务成功: {data}"
            elif data and "今日已签到" in data.get("msg", ""):
                # 站点返回已签到，视为成功
                status = "success"
                result_message = f"今日已签到: {data.get('full_msg', data['msg'])}"
            else:
                status = "failed"
                result_message = f"任务失败: {data}"
    except Exception as exc:
        status = "failed"
        result_message = f"执行异常: {exc}"
        logger.exception(f"调度 {schedule_id} 执行异常")

    finished_at = datetime.now()
    duration_ms = int((finished_at - started_at).total_seconds() * 1000)

    execution_logger.log(
        schedule_id=schedule_id,
        status=status,
        result_message=result_message,
        started_at=started_at.isoformat(),
        finished_at=finished_at.isoformat(),
        duration_ms=duration_ms,
    )

    logger.info(f"调度 {schedule_id} 结束: status={status}, duration={duration_ms}ms")


def _disable_site_account(site_account_id: int, db_pool: SQLiteConnectionPool) -> None:
    """停用指定 site_account，防止无密码且 Cookie 失效的账号无限重试。"""
    try:
        conn = db_pool.get_connection()
        conn.execute("UPDATE site_accounts SET is_enabled = 0 WHERE id = ?", (site_account_id,))
        conn.commit()
    except Exception as exc:
        logger = setup_logger()
        logger.error(f"停用 site_account_id={site_account_id} 失败: {exc}")


def _is_network_error(exc: Exception) -> bool:
    """判断异常是否属于通信层失败（适合切别名重试）。"""
    if isinstance(exc, requests.exceptions.RequestException):
        return True
    msg = str(exc).lower()
    network_keywords = (
        "connection",
        "timeout",
        "dns",
        "refused",
        "resolve",
        "reset",
        "too many redirects",
        "no route",
        "network",
    )
    return any(kw in msg for kw in network_keywords)


def _try_login_with_aliases(
    adapter,
    site_account_id: int,
    base_url: str,
    aliases: list,
    username: str,
    password: str,
    cookie: str,
    schedule_id: int,
):
    """
    依次尝试主域名和别名登录，返回 (session, execution_url)。

    若主域名因通信失败无法登录，则尝试别名；
    若登录接口明确返回失败（如账号不存在），不再尝试别名。
    """
    logger = setup_logger()
    urls = [base_url] + list(aliases)

    for idx, url in enumerate(urls):
        is_primary = idx == 0
        try:
            session = adapter.login(site_account_id, url, username, password, cookie)
            if session is not None:
                execution_url = adapter.execution_url(url)
                if not is_primary:
                    logger.info(f"调度 {schedule_id} 通过别名登录成功: {url}")
                return session, execution_url
            # adapter 返回 None 表示站点接口正常但凭证/业务逻辑失败
            logger.warning(f"{'主域名' if is_primary else '别名'} 登录返回失败: {url}")
            # 明确失败时不再尝试别名
            return None, None
        except Exception as exc:
            if _is_network_error(exc) and not is_primary:
                logger.warning(f"别名登录通信失败: {url}, {exc}")
                continue
            if _is_network_error(exc) and is_primary and aliases:
                logger.warning(f"主域名通信失败，准备尝试别名: {exc}")
                continue
            logger.warning(f"{'主域名' if is_primary else '别名'} 登录异常: {exc}")
            return None, None

    return None, None


def _process_due_schedules(
    db_pool: SQLiteConnectionPool,
    scheduler: ThreadPoolScheduler,
    execution_logger: ExecutionLogger,
    crypto: PasswordCrypto,
) -> int:
    """
    扫描并触发所有到期的调度。

    返回本次触发的调度数量。
    """
    logger = setup_logger()
    now_str = format_datetime(datetime.now())
    conn = db_pool.get_connection()
    cursor = conn.execute(SQL_SCHEDULES, (now_str,))
    rows = cursor.fetchall()

    triggered = 0
    for row in rows:
        schedule_id = row["schedule_id"]
        schedule_type = row["schedule_type"]
        schedule_value = row["schedule_value"]
        next_run_at_raw = row["next_run_at"]

        # next_run_at 为空时先进行初始化，不立即触发（now 类型除外）
        if next_run_at_raw is None and schedule_type != "now":
            next_run_at = calculate_next_run_at(schedule_type, schedule_value)
            if next_run_at is None:
                logger.warning(
                    f"无法初始化调度: schedule_id={schedule_id}, "
                    f"type={schedule_type}, value={schedule_value}，禁用该调度"
                )
                conn.execute("UPDATE schedules SET is_enabled = 0 WHERE id = ?", (schedule_id,))
                conn.commit()
                continue

            conn.execute(
                "UPDATE schedules SET next_run_at = ? WHERE id = ?",
                (format_datetime(next_run_at), schedule_id),
            )
            conn.commit()
            logger.info(
                f"初始化调度: schedule_id={schedule_id}, type={schedule_type}, "
                f"next_run_at={format_datetime(next_run_at)}"
            )
            continue

        if schedule_type == "now":
            # now 类型为一次性调度，执行后禁用，避免循环重复触发
            new_next_run_at = None
            is_enabled = 0
            logger.info(f"触发一次性调度: schedule_id={schedule_id}")
        else:
            new_next_run_at = calculate_next_run_at(
                schedule_type, schedule_value,
                force_tomorrow=(schedule_type == "window"),
            )
            if new_next_run_at is None:
                logger.warning(
                    f"无法计算下次执行时间: schedule_id={schedule_id}, "
                    f"type={schedule_type}, value={schedule_value}，禁用该调度"
                )
                is_enabled = 0
                new_next_run_at = None
            else:
                is_enabled = 1
                logger.info(
                    f"触发调度: schedule_id={schedule_id}, type={schedule_type}, "
                    f"next_run_at={format_datetime(new_next_run_at)}"
                )

        conn.execute(
            "UPDATE schedules SET next_run_at = ?, is_enabled = ? WHERE id = ?",
            (format_datetime(new_next_run_at) if new_next_run_at else None, is_enabled, schedule_id),
        )
        conn.commit()

        scheduler.submit(execute_schedule, row, db_pool, execution_logger, crypto)
        triggered += 1

    return triggered


def main() -> None:
    """主入口。"""
    parser = argparse.ArgumentParser(description="zibllCrawler 主程序")
    parser.add_argument(
        "--detect",
        metavar="FILE",
        help="批量检测站点适配性（每行一个 URL），检测完成后退出，不执行业务调度",
    )
    parser.add_argument(
        "--detect-output",
        default="compatible_sites.txt",
        help="批量检测结果输出文件路径（默认: compatible_sites.txt）",
    )
    parser.add_argument(
        "--detect-workers",
        type=int,
        default=4,
        help="批量检测并发数（默认: 4）",
    )
    parser.add_argument(
        "--proxy",
        metavar="ADDR",
        help="全局代理地址，如 host:port 或 http://host:port",
    )
    parser.add_argument(
        "--proxy-fail-action",
        choices=["exit", "continue"],
        default="exit",
        help="代理测试失败时的行为：exit 结束运行，continue 禁用代理继续（默认: exit）",
    )
    parser.add_argument(
        "--proxy-test-url",
        default=None,
        help="代理可用性测试 URL；不提供则跳过可用性测试",
    )
    parser.add_argument(
        "--max-loops",
        type=int,
        default=0,
        help="限制调度循环次数，仅用于测试；默认 0 表示无限循环",
    )

    # 手动导入模式
    parser.add_argument(
        "--import",
        dest="interactive_import",
        action="store_true",
        help="进入交互式数据导入菜单，导入完成后退出",
    )
    parser.add_argument(
        "--import-site",
        action="store_true",
        help="非交互式导入网站",
    )
    parser.add_argument(
        "--import-task",
        action="store_true",
        help="非交互式导入任务",
    )
    parser.add_argument(
        "--import-account",
        action="store_true",
        help="非交互式导入账号并绑定网站与任务",
    )
    parser.add_argument(
        "--import-timeout",
        type=int,
        default=300,
        help="交互式导入超时时间（秒），默认 300",
    )

    # 导入网站参数
    parser.add_argument("--site-url", help="导入网站: 主 URL")
    parser.add_argument("--site-name", default="", help="导入网站: 站点名称")
    parser.add_argument("--site-aliases", default="", help="导入网站: 别名 URL（逗号分隔）")
    parser.add_argument("--site-test-username", default="", help="导入网站: 测试账号用户名")
    parser.add_argument("--site-test-password", default="", help="导入网站: 测试账号密码")

    # 导入任务参数
    parser.add_argument("--task-name", help="导入任务: TASK_NAME")
    parser.add_argument("--task-module", help="导入任务: 业务模块名（如 checkin_business）")
    parser.add_argument("--task-func", help="导入任务: 函数名（如 perform_checkin）")
    parser.add_argument("--task-desc", default="", help="导入任务: 描述")

    # 导入账号参数
    parser.add_argument("--account-username", help="导入账号: 用户名")
    parser.add_argument("--account-password", help="导入账号: 密码（留空表示仅使用 Cookie）")
    parser.add_argument("--account-cookie", help="导入账号: 浏览器 Cookie 字符串（留空表示使用密码登录）")
    parser.add_argument("--account-site", help="导入账号: 绑定网站 URL")
    parser.add_argument("--account-task", help="导入账号: 绑定任务名")
    parser.add_argument(
        "--schedule-type",
        default="now",
        help="导入账号: 调度类型 [now/fixed/window/interval]，默认 now",
    )
    parser.add_argument("--schedule-value", default=None, help="导入账号: 调度值")

    args = parser.parse_args()

    logger = setup_logger()

    # 环境检测与初次运行初始化
    try:
        from core.env_check import EnvironmentError, handle_failure, run_check

        run_check()
    except EnvironmentError as exc:
        sys.exit(handle_failure(exc))
    except ImportError as exc:
        logger.error(f"无法导入环境检测模块，可能缺少必要依赖: {exc}")
        print("\n===== 环境检测失败 =====", file=sys.stderr)
        print(f"无法导入环境检测模块: {exc}", file=sys.stderr)
        print("修复建议: 请激活虚拟环境并执行 pip install -r requirements.txt", file=sys.stderr)
        print("========================\n", file=sys.stderr)
        sys.exit(1)

    # 检测模式：一次性任务，完成后直接退出
    if args.detect:
        logger.info(f"进入站点适配性检测模式: 输入={args.detect}, 输出={args.detect_output}, 并发={args.detect_workers}")
        detect_sites(args.detect, args.detect_output, max_workers=args.detect_workers)
        logger.info("站点适配性检测完成，程序退出")
        return

    # 手动导入模式：导入完成后直接退出，不进入主循环
    import_mode = args.interactive_import or args.import_site or args.import_task or args.import_account
    if import_mode:
        db_pool = SQLiteConnectionPool("sqldb/zibllcrawler.db")
        crypto = PasswordCrypto()
        try:
            importer = Importer(db_pool, crypto, interactive_timeout=args.import_timeout)
            importer.run(args)
        finally:
            db_pool.close_all()
        logger.info("导入完成，程序退出")
        return

    # 全局代理配置
    if args.proxy:
        configure_proxy(
            args.proxy,
            fail_action=args.proxy_fail_action,
            test_url=args.proxy_test_url,
        )

    logger.info("启动 zibllCrawler")

    db_pool = SQLiteConnectionPool("sqldb/zibllcrawler.db")
    scheduler = ThreadPoolScheduler(max_workers=4)
    execution_logger = ExecutionLogger(db_pool)
    crypto = PasswordCrypto()

    loop_count = 0
    try:
        while True:
            if args.max_loops and loop_count >= args.max_loops:
                logger.info(f"达到测试最大循环次数 {args.max_loops}，结束运行")
                break
            loop_count += 1

            triggered = _process_due_schedules(db_pool, scheduler, execution_logger, crypto)
            if triggered:
                logger.info(f"本轮触发 {triggered} 个调度")

            # 等待 60 秒后再进行下一轮扫描
            logger.debug("调度循环进入 60 秒等待")
            time.sleep(60)

    finally:
        scheduler.shutdown()
        db_pool.close_all()
        logger.info("zibllCrawler 已关闭")


if __name__ == "__main__":
    main()
