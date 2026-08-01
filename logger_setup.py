import logging
import os
import shutil
import tempfile
import threading
import yaml


class PrependFileHandler(logging.Handler):
    """
    将最新日志写入文件顶部的 Handler。

    普通 FileHandler 是追加写入，最新内容在末尾，阅读时需要翻到底部。
    该 Handler 每次写入时先把新记录放在文件开头，再拼接旧内容，
    便于直接查看最新日志。由于每次需要全量读写文件，仅适用于日志量
    不大的场景；日志量极大时应改用追加式 Handler。
    """

    def __init__(self, filename: str, encoding: str = "utf-8"):
        super().__init__()
        self.filename = filename
        self.encoding = encoding
        self.terminator = "\n"
        self._lock = threading.Lock()

    def emit(self, record):
        try:
            msg = self.format(record) + self.terminator
            with self._lock:
                # 使用临时文件再原子替换，避免写入过程中崩溃导致日志丢失
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding=self.encoding,
                    delete=False,
                    dir=os.path.dirname(self.filename) or ".",
                ) as tmp:
                    tmp.write(msg)
                    if os.path.exists(self.filename):
                        with open(self.filename, "r", encoding=self.encoding) as old:
                            shutil.copyfileobj(old, tmp)
                    tmp_name = tmp.name
                shutil.move(tmp_name, self.filename)
        except Exception:
            self.handleError(record)


def setup_logger(config_file: str = "config/settings.yaml"):
    """配置日志系统（全局单例）"""
    logger = logging.getLogger("zibllcrawler")
    if logger.handlers:  # 防止重复配置
        return logger

    # 默认配置（如果文件不存在或读取出错）
    default_config = {
        "logging": {
            "level": "INFO",
            "console": True,
            "file": None
        }
    }

    try:
        with open(config_file, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if config is None:  # 文件为空
            config = default_config
    except FileNotFoundError:
        print(f"⚠️ 配置文件不存在: {config_file}，使用默认配置")
        config = default_config

    log_config = config.get("logging", default_config["logging"])
    level = getattr(logging, log_config.get("level", "INFO").upper())

    if log_file := log_config.get("file"):  # 使用海象运算符
        os.makedirs(os.path.dirname(log_file), exist_ok=True)

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    logger.setLevel(level)

    if log_config.get("console", True):
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    if log_file:
        file_handler = PrependFileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger