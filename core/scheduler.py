"""全局线程池调度器。

基于 concurrent.futures.ThreadPoolExecutor，提供任务提交与关闭接口。
所有业务/登录任务通过本调度器提交，以便统一控制并发数量与生命周期。
"""

import logging
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, Callable


class ThreadPoolScheduler:
    """基于 ThreadPoolExecutor 的全局线程池。"""

    def __init__(self, max_workers: int = 4):
        self.logger = logging.getLogger("zibllcrawler.core.scheduler")
        self.max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self.logger.info(f"初始化线程池，max_workers={max_workers}")

    def submit(self, fn: Callable[..., Any], *args, **kwargs) -> Future:
        """提交任务到线程池并返回 Future。"""
        self.logger.debug(f"提交任务: {fn.__name__}")
        return self._executor.submit(fn, *args, **kwargs)

    def shutdown(self, wait: bool = True) -> None:
        """关闭线程池，等待所有任务完成。"""
        self.logger.info("正在关闭线程池...")
        self._executor.shutdown(wait=wait)
        self.logger.info("线程池已关闭")

    def __enter__(self) -> "ThreadPoolScheduler":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.shutdown()
