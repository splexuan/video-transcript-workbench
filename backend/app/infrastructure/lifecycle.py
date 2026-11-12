"""应用退出钩子。

自动更新需要「先让当前进程正常退出，再由外部脚本替换程序目录」。接口层拿不到
uvicorn.Server，也不该直接调用 os._exit（数据库与日志都来不及收尾），于是由
`run.py` 在启动时注册一个退出回调，更新模块只负责请求退出。

没有注册回调时（开发服务器、测试）请求退出是空操作——更新前会先检查打包状态，
正常也不会走到这里。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_callbacks: list[Callable[[], None]] = []


def register_shutdown(callback: Callable[[], None]) -> None:
    """注册退出动作；重复注册同一个函数不会叠加。"""

    with _lock:
        if callback not in _callbacks:
            _callbacks.append(callback)


def clear_shutdown() -> None:
    """主要给测试用：把回调清空，避免用例之间互相影响。"""

    with _lock:
        _callbacks.clear()


def request_shutdown(delay: float = 0.0) -> bool:
    """请求应用退出；返回是否真的有回调可执行。

    `delay` 用来把 HTTP 响应先送出去：接口返回之后进程再开始收尾，浏览器不会
    看到一个被中断的请求。退出在后台线程里做，调用方（接口）立即返回。
    """

    with _lock:
        callbacks = list(_callbacks)
    if not callbacks:
        return False

    def run() -> None:
        if delay > 0:
            time.sleep(delay)
        for callback in callbacks:
            try:
                callback()
            except Exception:  # noqa: BLE001 退出路径上不能让单个回调挡住其余动作
                logger.exception("执行退出回调失败")

    threading.Thread(target=run, name="app-shutdown", daemon=True).start()
    return True
