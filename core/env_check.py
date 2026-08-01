"""环境检测与初次运行初始化模块。

职责：
1. 检测当前 Python 环境是否满足项目最小运行条件。
2. 根据 `config/settings.yaml` 中的 `initialization.status` 判断是否为初次运行。
3. 初次运行时辅助完成数据库初始化，并在配置文件中标记初始化状态。
4. 已初始化状态下快速跳过，避免重复检测。
5. 检测不通过时说明原因、给出修复建议，并安全结束运行。

使用方式：
- 由 `main.py` 在启动时无条件调用 `run_check()`。
- 是否需要执行完整检测由 `config/settings.yaml` 中的 `initialization.status` 控制：
  - `"initialized"`：跳过检测。
  - `"pending"` 或字段缺失：执行完整检测与初始化。
- 如需重新触发检测，可手动将 `initialization.status` 改为 `"pending"` 或删除该字段。
"""

import logging
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import yaml


logger = logging.getLogger("zibllcrawler.core.env_check")

# 最低 Python 版本要求
MIN_PYTHON_VERSION = (3, 10)

# 项目直接依赖的第三方包（与 requirements.txt 保持一致）
REQUIRED_PACKAGES = [
    ("requests", "requests"),
    ("yaml", "PyYAML"),
    ("cryptography", "cryptography"),
]

# 项目正常运行所必需的核心文件（相对项目根目录）
REQUIRED_FILES: List[str] = [
    "main.py",
    "logger_setup.py",
    "config/settings.yaml",
    "core/db_pool.py",
    "core/scheduler.py",
    "core/business_loader.py",
    "core/execution_logger.py",
    "core/password_crypto.py",
    "core/schedule_planner.py",
    "adapters/base_login.py",
    "adapters/captcha_solver.py",
    "adapters/factory.py",
    "sqldb/init_db.py",
]

# 数据库默认路径（相对项目根目录）
DEFAULT_DB_PATH = "sqldb/zibllcrawler.db"
DEFAULT_CONFIG_PATH = "config/settings.yaml"


class EnvironmentError(Exception):
    """环境检测失败时抛出的异常，携带可读原因。"""

    def __init__(self, reason: str, fix_suggestion: str):
        self.reason = reason
        self.fix_suggestion = fix_suggestion
        super().__init__(f"{reason}\n修复建议: {fix_suggestion}")


def _project_root() -> Path:
    """返回项目根目录，即本文件所在目录的父目录。"""
    return Path(__file__).resolve().parent.parent


def _load_settings(config_path: Path) -> Tuple[dict, bool]:
    """
    加载 settings.yaml。

    Returns:
        (settings_dict, 文件是否存在)
    """
    if not config_path.exists():
        return {}, False
    try:
        with config_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data, True
    except Exception as exc:
        raise EnvironmentError(
            reason=f"配置文件解析失败: {config_path}",
            fix_suggestion=f"请检查 YAML 语法是否正确，错误信息: {exc}",
        ) from exc


def _save_settings(config_path: Path, settings: dict) -> None:
    """保存 settings.yaml，自动补充分隔注释。"""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(settings, f, sort_keys=False, allow_unicode=True)


def _ensure_initialization_section(settings: dict) -> dict:
    """确保 settings 中包含 initialization 节，缺失则创建。"""
    if "initialization" not in settings or not isinstance(settings["initialization"], dict):
        settings["initialization"] = {}
    return settings


def _is_initialized(settings: dict) -> bool:
    """判断当前是否为已初始化状态。"""
    init_section = settings.get("initialization", {})
    return init_section.get("status") == "initialized"


def check_python_version() -> None:
    """检查 Python 版本是否满足最低要求。"""
    if sys.version_info < MIN_PYTHON_VERSION:
        raise EnvironmentError(
            reason=(
                f"Python 版本过低: 当前 {sys.version_info.major}.{sys.version_info.minor}."
                f"{sys.version_info.micro}，"
                f"要求 >= {MIN_PYTHON_VERSION[0]}.{MIN_PYTHON_VERSION[1]}"
            ),
            fix_suggestion="请升级 Python 到 3.10 或更高版本后再运行本项目。",
        )


def check_required_packages() -> None:
    """检查项目直接依赖的第三方包是否已安装。"""
    missing = []
    for import_name, package_name in REQUIRED_PACKAGES:
        try:
            __import__(import_name)
        except ImportError:
            missing.append(package_name)

    if missing:
        raise EnvironmentError(
            reason=f"缺少必要的第三方依赖: {', '.join(missing)}",
            fix_suggestion=(
                "请激活虚拟环境后执行: pip install -r requirements.txt\n"
                "如果尚未创建虚拟环境，可执行: python -m venv .venv && "
                "source .venv/bin/activate && pip install -r requirements.txt"
            ),
        )


def check_required_files(project_root: Path) -> None:
    """检查项目核心文件是否齐全。"""
    missing = []
    for rel_path in REQUIRED_FILES:
        full_path = project_root / rel_path
        if not full_path.exists():
            missing.append(rel_path)

    if missing:
        raise EnvironmentError(
            reason=f"项目文件缺失或路径异常: {', '.join(missing)}",
            fix_suggestion=(
                "请确认项目源码完整，或从仓库重新拉取。\n"
                "如需测试本模块的检测能力，可将文件临时重命名为 .bak 后缀，"
                "测试结束后恢复即可，切勿删除项目文件。"
            ),
        )


def check_database(project_root: Path, db_path: str) -> None:
    """
    检查数据库文件是否存在且可访问。

    数据库文件不存在时不会报错，而是留给初始化流程创建。
    """
    full_db_path = project_root / db_path
    if full_db_path.exists() and not full_db_path.is_file():
        raise EnvironmentError(
            reason=f"数据库路径异常（非文件）: {full_db_path}",
            fix_suggestion="请检查 sqldb/ 目录，删除异常路径后重新运行。",
        )


def initialize_database(project_root: Path) -> None:
    """调用 sqldb/init_db.py 初始化数据库。"""
    init_script = project_root / "sqldb" / "init_db.py"
    try:
        import subprocess

        result = subprocess.run(
            [sys.executable, str(init_script)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr or result.stdout or "init_db.py 无输出")
        logger.info("数据库初始化完成")
    except Exception as exc:
        raise EnvironmentError(
            reason=f"数据库初始化失败: {exc}",
            fix_suggestion=(
                "请检查 sqldb/init_db.py 是否可正常执行，\n"
                "或手动运行: python sqldb/init_db.py"
            ),
        ) from exc


def mark_initialized(config_path: Path, settings: dict) -> None:
    """在配置文件中标记初始化已完成。"""
    settings = _ensure_initialization_section(settings)
    settings["initialization"]["status"] = "initialized"
    _save_settings(config_path, settings)
    logger.info(f"已更新初始化状态: {config_path}")


def run_check(
    config_path: Optional[str] = None,
    db_path: Optional[str] = None,
    auto_init_db: bool = True,
) -> bool:
    """
    执行环境检测与初次运行初始化。

    Args:
        config_path: 配置文件路径，默认 config/settings.yaml。
        db_path: 数据库路径，默认 sqldb/zibllcrawler.db。
        auto_init_db: 初次运行时是否自动调用 sqldb/init_db.py 初始化数据库。

    Returns:
        True 表示检测通过/已初始化，可继续运行主程序；
        False 表示检测未通过，调用方应安全结束运行。
    """
    config_path = config_path or DEFAULT_CONFIG_PATH
    db_path = db_path or DEFAULT_DB_PATH
    project_root = _project_root()
    full_config_path = project_root / config_path

    try:
        settings, config_exists = _load_settings(full_config_path)
    except EnvironmentError:
        raise

    if not config_exists:
        logger.warning(f"配置文件不存在: {full_config_path}，将视为首次运行")
        settings = {}

    if _is_initialized(settings):
        logger.debug("项目已初始化，跳过环境检测")
        return True

    logger.info("首次运行或初始化状态为 pending，开始环境检测")

    # 依次执行各项检测，任意一项失败即抛出 EnvironmentError
    check_python_version()
    check_required_packages()
    check_required_files(project_root)
    check_database(project_root, db_path)

    logger.info("基础环境检测通过")

    full_db_path = project_root / db_path
    if auto_init_db and not full_db_path.exists():
        logger.info("数据库文件不存在，开始初始化数据库")
        initialize_database(project_root)
    elif full_db_path.exists():
        logger.info("数据库文件已存在，跳过自动初始化")

    mark_initialized(full_config_path, settings)
    logger.info("环境检测与初始化完成，继续运行主程序")
    return True


def handle_failure(exc: EnvironmentError) -> int:
    """统一输出检测失败信息并返回退出码。"""
    logger.error(f"环境检测未通过: {exc.reason}")
    logger.error(exc.fix_suggestion)
    print("\n===== 环境检测失败 =====", file=sys.stderr)
    print(f"原因: {exc.reason}", file=sys.stderr)
    print(f"修复建议: {exc.fix_suggestion}", file=sys.stderr)
    print("========================\n", file=sys.stderr)
    return 1
